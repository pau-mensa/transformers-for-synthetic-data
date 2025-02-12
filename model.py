import os
import math
import time
import inspect
from dataclasses import dataclass
import torch
import torch.nn as nn
from torch.nn import functional as F

class LayerNorm(nn.Module):
    """ LayerNorm but with an optional bias. PyTorch doesn't support simply bias=False """

    def __init__(self, ndim, bias):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(ndim))
        self.bias = nn.Parameter(torch.zeros(ndim)) if bias else None

    def forward(self, input):
        return F.layer_norm(input, self.weight.shape, self.weight, self.bias, 1e-5)
    
    
class SelfAttention(nn.Module):

    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0
        # key, query, value projections for all heads, but in a batch
        self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd, bias=config.bias)
        # output projection
        self.c_proj = nn.Linear(config.n_embd, config.n_embd, bias=config.bias)
        # regularization
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        self.dropout = config.dropout
        self.mask = config.mask
        # flash attention is way faster than MultiHeadAttention, requires Pytorch >= 2.0
        assert hasattr(torch.nn.functional, 'scaled_dot_product_attention'), "Flash Attention requires PyTorch >= 2.0"


    def forward(self, x):
        B, T, C = x.size() # batch size, sequence length, embedding dimensionality (n_embd)

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        q, k, v  = self.c_attn(x).split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        y = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=None, dropout_p=self.dropout if self.training else 0, is_causal=self.mask)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y
    
class CrossAttention(nn.Module):

    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0
        # key, query, value projections for all heads, but in a batch
        self.linear_enc = nn.Linear(config.n_embd*2, config.n_embd, bias=config.bias)
        self.c_attn = nn.Linear(config.n_embd, config.n_embd, bias=config.bias)
        # output projection
        self.c_proj = nn.Linear(config.n_embd, config.n_embd, bias=config.bias)
        # regularization
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        self.dropout = config.dropout
        self.mask = config.mask
        assert hasattr(torch.nn.functional, 'scaled_dot_product_attention'), "Flash Attention requires PyTorch >= 2.0"
        

    def forward(self, x, encoded_data):
        B, T, C = x.size() # batch size, sequence length, embedding dimensionality (n_embd)

        # In cross attention the key and value are calculated using the data from the encoder, while the query value is calculated using the decoder data.
        # The rest of the code is the same.
        q = self.c_attn(x)
        k = self.c_attn(encoded_data)
        v = self.c_attn(encoded_data)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        y = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=None, dropout_p=self.dropout if self.training else 0, is_causal=self.mask)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y
    
    
class MLP(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.c_fc    = nn.Linear(config.n_embd, 4 * config.n_embd, bias=config.bias)
        self.gelu    = nn.GELU()
        self.c_proj  = nn.Linear(4 * config.n_embd, config.n_embd, bias=config.bias)
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        x = self.dropout(x)
        return x
    
    
class Block(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.ln_1 = LayerNorm(config.n_embd, bias=config.bias)
        self.attn = SelfAttention(config)
        self.ln_2 = LayerNorm(config.n_embd, bias=config.bias)
        self.cross_attn = CrossAttention(config)
        self.ln_3 = LayerNorm(config.n_embd, bias=config.bias)
        self.ln_4 = LayerNorm(config.n_embd, bias=config.bias)
        self.linear = nn.Linear(config.n_embd, config.n_embd)
        self.mlp = MLP(config)

    def forward(self, x, encoded_x):
        # We add x to each layer to skip connections.
        x = x + self.attn(self.ln_1(x))
        x = x + self.ln_2(self.cross_attn(x, encoded_x))
        x = x + self.mlp(self.ln_3(x))
        return x
    
class EncoderBlock(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = SelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x
    
class Generator(nn.Module):

    def __init__(self, config, encoder_config):
        super().__init__()
        assert config.vocab_size is not None
        assert config.block_size is not None
        assert config.n_layer == encoder_config.n_layer
        self.config = config
        self.encoder_config = encoder_config
        self.base_std = 1/np.sqrt(config.n_embd) # The base std for initialization is calculated by taking the inverse of the sqrt of the embedding size
        
        # We do not need token embeddings in the encoder.
        self.encoder = nn.ModuleDict(dict(
            f_ln = nn.Linear(encoder_config.block_size, encoder_config.block_size),
            wpe = nn.Embedding(encoder_config.block_size, encoder_config.n_embd),
            drop = nn.Dropout(encoder_config.dropout),
            h = nn.ModuleList([EncoderBlock(encoder_config) for _ in range(encoder_config.n_layer)]),
            ln_f = LayerNorm(encoder_config.block_size, bias=encoder_config.bias),
        ))
        
        self.decoder = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd),
            wpe = nn.Embedding(config.block_size, config.n_embd),
            drop = nn.Dropout(config.dropout),
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            ln_f = LayerNorm(config.n_embd, bias=config.bias),
        ))
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        
        # This applies weight tying. It is useful to tie the weights of the head that generates the logit tokens with the token embedding layer.
        # The encoder does not need this since it does not generate tokens.
        self.decoder.wte.weight = self.lm_head.weight # https://paperswithcode.com/method/weight-tying

        # init all weights
        self.apply(self._init_weights)
        # apply special scaled init to the residual projections, per GPT-2 paper
        for pn, p in self.named_parameters():
            if pn.endswith('c_proj.weight'):
                torch.nn.init.normal_(p, mean=0.0, std=self.base_std/np.sqrt(5 * config.n_layer)) # Why 5? 3 operations on each Block + 2 operations on each Encoder Block

        # report number of parameters
        print("number of parameters: %.2fM" % (self.get_num_params()/1e6,))

    def get_num_params(self, non_embedding=True):
        """
        Return the number of parameters in the model.
        For non-embedding count (default), the position embeddings get subtracted.
        The token embeddings would too, except due to the parameter sharing these
        params are actually used as weights in the final layer, so we include them.
        """
        n_params = sum(p.numel() for p in self.parameters())
        if non_embedding:
            n_params -= self.decoder.wpe.weight.numel()
            n_params -= self.encoder.wpe.weight.numel()
        return n_params

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=self.base_std)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=self.base_std)

    def forward(self, idx, vols, targets=None):
        device = idx.device
        b, t = idx.size()
        b2, t2 = vols.size()
        assert t <= self.config.block_size, f"Cannot forward sequence of length {t}, block size is only {self.config.block_size}"
        pos = torch.arange(0, t, dtype=torch.long, device=device) # shape (t)
        pos_vol = torch.arange(0, t2, dtype=torch.long, device=device) # shape (t)

        tok_emb = self.decoder.wte(idx) # token embeddings of shape (b, t, n_embd)
        pos_emb = self.decoder.wpe(pos) # position embeddings of shape (t, n_embd)
        
        pos_emb_vol = self.encoder.wpe(pos_vol) # position embeddings of shape (t, n_embd)
        x = self.decoder.drop(tok_emb + pos_emb)
        
        ln_x_vol = self.encoder.ln_f(self.encoder.f_ln(vols)) # We substitute the embedding lookup for a linear transformation
        x_vol = self.encoder.drop(ln_x_vol.unsqueeze(2) + pos_emb_vol.unsqueeze(0)) # We fix the dimensions so the result is of shape (b, t, n_embd)

        for idx in range(self.config.n_layer):
            encoded_x = self.encoder.h[idx](x_vol) # For every block we encode the data and then pass it to the decoder
            x = self.decoder.h[idx](x, encoded_x)
        x = self.decoder.ln_f(x)

        if targets is not None:
            # if we are given some desired targets also calculate the loss
            logits = self.lm_head(x)
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
        else:
            # during inference we only need to apply the head to the temporal dimension
            logits = self.lm_head(x[:, [-1], :]) # note: using list [-1] to preserve the time dim
            loss = None

        return logits, loss

    def crop_block_size(self, block_size):
        # model surgery to decrease the block size if necessary
        # e.g. we may load the GPT2 pretrained model checkpoint (block size 1024)
        # but want to use a smaller block size for some smaller, simpler model
        assert block_size <= self.config.block_size
        self.config.block_size = block_size
        self.decoder.wpe.weight = nn.Parameter(self.decoder.wpe.weight[:block_size])
        for block in self.decoder.h:
            if hasattr(block.attn, 'bias'):
                block.attn.bias = block.attn.bias[:,:,:block_size,:block_size]
    
    def configure_optimizers(self, weight_decay, learning_rate, betas, device_type):
        # start with all of the candidate parameters
        param_dict = {pn: p for pn, p in self.named_parameters()}
        # filter out those that do not require grad
        param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
        # create optim groups. Any parameters that is 2D will be weight decayed, otherwise no.
        # i.e. all weight tensors in matmuls + embeddings decay, all biases and layernorms don't.
        decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
        nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {'params': nodecay_params, 'weight_decay': 0.0}
        ]
        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)
        print(f"num decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} parameters")
        print(f"num non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} parameters")
        # Create AdamW optimizer and use the fused version if it is available
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and device_type == 'cuda'
        extra_args = dict(fused=True) if use_fused else dict()
        optimizer = torch.optim.AdamW(optim_groups, lr=learning_rate, betas=betas, **extra_args)
        print(f"using fused AdamW: {use_fused}")

        return optimizer
    
    @torch.no_grad()
    def generate(self, idx, vols, bins, rets, max_new_tokens, temperature=1.0, top_k=None):
        """
        Take a conditioning sequence of indices idx (LongTensor of shape (b,t)) and complete
        the sequence max_new_tokens times, feeding the predictions back into the model each time.
        Most likely you'll want to make sure to be in model.eval() mode of operation for this.
        """
        assert idx.size(1) >= self.config.block_size and vols.size(1) >= self.encoder_config.block_size
        for _ in range(max_new_tokens):
            # if the sequence context is growing too long we must crop it at block_size
            idx_cond = idx if idx.size(1) <= self.config.block_size else idx[:, -self.config.block_size:]
            idx_vols = int(np.clip(idx.size(1) - self.config.block_size, 0, np.inf))
            vols_cond = vols if vols.size(1) <= self.encoder_config.block_size else vols[:, idx_vols:idx_vols+self.encoder_config.block_size]
            # forward the model to get the logits for the index in the sequence
            logits, _ = self(idx_cond, vols_cond)
            # pluck the logits at the final step and scale by desired temperature
            logits = logits[:, -1, :] / temperature
            # optionally crop the logits to only the top k options
            if top_k is not None:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = -float('Inf')
            # apply softmax to convert logits to (normalized) probabilities
            probs = F.softmax(logits, dim=-1)
            # sample from the distribution
            idx_next = torch.multinomial(probs, num_samples=1)
            # append sampled index to the running sequence and continue
            idx = torch.cat((idx, idx_next), dim=1)
            ret = torch.from_numpy(np.array([bins[idx_next.item()]], dtype=np.float32))
            ret = ret.to(device)
            rets = torch.cat((rets, ret))
            mean_ret = torch.mean(rets[-252:])
            if idx.size(1) > vols.size(1):
                print("No future variance provided, auto calculating...")
                # This means we have more returns than variances, so we append the variance of the last token.
                var = torch.abs(ret - mean_ret)[None, ...]
                vols = torch.cat((vols, torch.where(var < 0.03, 0, var)), dim=1)

        return idx