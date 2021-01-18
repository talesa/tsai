# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/108c_models.TSTPlus.ipynb (unless otherwise specified).

__all__ = ['SinCosPosEncoding', 'Coord2dPosEncoding', 'Coord1dPosEncoding', 'ScaledDotProductAttention',
           'MultiHeadAttention', 'TSTEncoderLayer', 'TSTEncoder', 'TSTPlus', 'MultiTSTPlus']

# Cell
from ..imports import *
from ..utils import *
from .layers import *
from .utils import *

# Cell
def SinCosPosEncoding(q_len, d_model, normalize=True):
    pe = torch.zeros(q_len, d_model, device=default_device())
    position = torch.arange(0, q_len).unsqueeze(1)
    div_term = torch.exp(torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model))
    pe[:, 0::2] = torch.sin(position * div_term)
    pe[:, 1::2] = torch.cos(position * div_term)
    if normalize:
        pe = pe - pe.mean()
        pe = pe / (pe.std() * 10)
    return pe.to(device=device)

# Cell
def Coord2dPosEncoding(q_len, d_model, exponential=False, normalize=True, eps=1e-3, verbose=False, device=default_device()):
    x = .5 if exponential else 1
    i = 0
    for i in range(100):
        cpe = 2 * (torch.linspace(0, 1, q_len).reshape(-1, 1) ** x) * (torch.linspace(0, 1, d_model).reshape(1, -1) ** x) - 1
        pv(f'{i:4.0f}  {x:5.3f}  {cpe.mean():+6.3f}', verbose)
        if abs(cpe.mean()) <= eps: break
        elif cpe.mean() > eps: x += .001
        else: x -= .001
        i += 1
    if normalize:
        cpe = cpe - cpe.mean()
        cpe = cpe / (cpe.std() * 10)
    return cpe.to(device=device)

# Cell
def Coord1dPosEncoding(q_len, exponential=False, normalize=True, device=default_device()):
    cpe = (2 * (torch.linspace(0, 1, q_len).reshape(-1, 1)**(.5 if exponential else 1)) - 1)
    if normalize:
        cpe = cpe - cpe.mean()
        cpe = cpe / (cpe.std() * 10)
    return cpe.to(device=device)

# Cell
class ScaledDotProductAttention(Module):
    def __init__(self, d_k:int, res_attention:bool=False): self.d_k,self.res_attention = d_k,res_attention
    def forward(self, q:Tensor, k:Tensor, v:Tensor, prev:Optional[Tensor]=None, attn_mask:Optional[Tensor]=None):

        # MatMul (q, k) - similarity scores for all pairs of positions in an input sequence
        scores = torch.matmul(q, k)                                    # scores : [bs x n_heads x q_len x q_len]

        # Scale
        scores = scores / (self.d_k ** 0.5)

        # Attention mask (optional)
        if attn_mask is not None:                                     # mask with shape [q_len x q_len]
            if attn_mask.dtype == torch.bool:
                scores.masked_fill_(attn_mask, float('-inf'))
            else:
                scores += attn_mask

        # SoftMax
        if prev is not None: scores = scores + prev

        attn = F.softmax(scores, dim=-1)                               # attn   : [bs x n_heads x q_len x q_len]

        # MatMul (attn, v)
        context = torch.matmul(attn, v)                                # context: [bs x n_heads x q_len x d_v]

        if self.res_attention: return context, attn, scores
        else: return context, attn

# Cell
class MultiHeadAttention(Module):
    def __init__(self, d_model:int, n_heads:int, d_k:int, d_v:int, res_attention:bool=False):
        r"""
        Input shape:  Q, K, V:[batch_size (bs) x q_len x d_model], mask:[q_len x q_len]
        """
        self.n_heads, self.d_k, self.d_v = n_heads, d_k, d_v

        self.W_Q = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_K = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_V = nn.Linear(d_model, d_v * n_heads, bias=False)

        self.W_O = nn.Linear(n_heads * d_v, d_model, bias=False)

        self.res_attention = res_attention

    def forward(self, Q:Tensor, K:Tensor, V:Tensor, prev:Optional[Tensor]=None, attn_mask:Optional[Tensor]=None):

        bs = Q.size(0)

        # Linear (+ split in multiple heads)
        q_s = self.W_Q(Q).view(bs, -1, self.n_heads, self.d_k).transpose(1,2)       # q_s    : [bs x n_heads x q_len x d_k]
        k_s = self.W_K(K).view(bs, -1, self.n_heads, self.d_k).permute(0,2,3,1)     # k_s    : [bs x n_heads x d_k x q_len] - transpose(1,2) + transpose(2,3)
        v_s = self.W_V(V).view(bs, -1, self.n_heads, self.d_v).transpose(1,2)       # v_s    : [bs x n_heads x q_len x d_v]

        # Scaled Dot-Product Attention (multiple heads)
        if self.res_attention:
            context, attn, scores = ScaledDotProductAttention(self.d_k, self.res_attention)(q_s, k_s, v_s, prev=prev, attn_mask=attn_mask)
        else:
            context, attn = ScaledDotProductAttention(self.d_k)(q_s, k_s, v_s, attn_mask=attn_mask)
        # context: [bs x n_heads x q_len x d_v], attn: [bs x n_heads x q_len x q_len]

        # Concat
        context = context.transpose(1, 2).contiguous().view(bs, -1, self.n_heads * self.d_v) # context: [bs x q_len x n_heads * d_v]

        # Linear
        output = self.W_O(context)                                                           # context: [bs x q_len x d_model]

        if self.res_attention: return output, attn, scores
        else: return output, attn                                                            # output: [bs x q_len x d_model]


# Cell
class TSTEncoderLayer(Module):
    def __init__(self, q_len:int, d_model:int, n_heads:int, d_k:Optional[int]=None, d_v:Optional[int]=None, d_ff:int=256,
                 res_dropout:float=0.1, activation:str="gelu", res_attention:bool=False):

        assert d_model // n_heads, f"d_model ({d_model}) must be divisible by n_heads ({n_heads})"
        d_k = ifnone(d_k, d_model // n_heads)
        d_v = ifnone(d_v, d_model // n_heads)

        # Multi-Head attention
        self.res_attention = res_attention
        self.self_attn = MultiHeadAttention(d_model, n_heads, d_k, d_v, res_attention=res_attention)

        # Add & Norm
        self.dropout_attn = nn.Dropout(res_dropout)
        self.batchnorm_attn = nn.BatchNorm1d(q_len)

        # Position-wise Feed-Forward
        self.ff = nn.Sequential(nn.Linear(d_model, d_ff), self._get_activation_fn(activation), nn.Linear(d_ff, d_model))

        # Add & Norm
        self.dropout_ffn = nn.Dropout(res_dropout)
        self.batchnorm_ffn = nn.BatchNorm1d(q_len)

    def forward(self, src:Tensor, prev:Optional[Tensor]=None, attn_mask:Optional[Tensor]=None) -> Tensor:

        # Multi-Head attention sublayer
        ## Multi-Head attention
        if self.res_attention:
            src2, attn, scores = self.self_attn(src, src, src, prev, attn_mask=attn_mask)
        else:
            src2, attn = self.self_attn(src, src, src, attn_mask=attn_mask)
        ## Add & Norm
        src = src + self.dropout_attn(src2) # Add: residual connection with residual dropout
        src = self.batchnorm_attn(src) # Norm: batchnorm

        # Feed-forward sublayer
        ## Position-wise Feed-Forward
        src2 = self.ff(src)
        ## Add & Norm
        src = src + self.dropout_ffn(src2) # Add: residual connection with residual dropout
        src = self.batchnorm_ffn(src) # Norm: batchnorm

        if self.res_attention:
            return src, scores
        else:
            return src

    def _get_activation_fn(self, activation):
        if activation.lower() == "relu": return nn.ReLU()
        elif activation.lower() == "gelu": return nn.GELU()
        raise ValueError(f'{activation} is not available. You can use "relu" or "gelu"')

# Cell
class TSTEncoder(Module):
    def __init__(self, encoder_layer, n_layers, res_attention:bool=False):
        self.layers = nn.ModuleList([deepcopy(encoder_layer) for i in range(n_layers)])
        self.res_attention = res_attention

    def forward(self, src:Tensor, attn_mask:Optional[Tensor]=None) -> Tensor:
        output = src
        scores = None
        if self.res_attention:
            for mod in self.layers: output, scores = mod(output, prev=scores, attn_mask=attn_mask)
            return output
        else:
            for mod in self.layers: output = mod(output, attn_mask=attn_mask)
            return output

# Cell
class TSTPlus(Module):
    def __init__(self, c_in:int, c_out:int, seq_len:int, max_seq_len:Optional[int]=512,
                 n_layers:int=3, d_model:int=128, n_heads:int=16, d_k:Optional[int]=None, d_v:Optional[int]=None,
                 d_ff:int=256, res_dropout:float=0.1, activation:str="gelu", res_attention:bool=True,
                 pe:str='normal', learn_pe:bool=True, flatten:bool=True, fc_dropout:float=0.,
                 concat_pool:bool=True, bn:bool=False, custom_head:Optional=None,
                 y_range:Optional[tuple]=None, verbose:bool=False, **kwargs):
        r"""TST (Time Series Transformer) is a Transformer that takes continuous time series as inputs.
        As mentioned in the paper, the input must be standardized by_var based on the entire training set.
        Args:
            c_in: the number of features (aka variables, dimensions, channels) in the time series dataset.
            c_out: the number of target classes.
            seq_len: number of time steps in the time series.
            max_seq_len: useful to control the temporal resolution in long time series to avoid memory issues. Default=512.
            d_model: total dimension of the model (number of features created by the model)
            n_heads:  parallel attention heads.
            d_k: size of the learned linear projection of queries and keys in the MHA. Usual values: 16-512. Default: None -> (d_model/n_heads) = 32.
            d_v: size of the learned linear projection of values in the MHA. Usual values: 16-512. Default: None -> (d_model/n_heads) = 32.
            d_ff: the dimension of the feedforward network model.
            res_dropout: amount of residual dropout applied in the encoder.
            activation: the activation function of intermediate layer, relu or gelu.
            res_attention: if True Residual MultiHeadAttention is applied.
            num_layers: the number of sub-encoder-layers in the encoder.
            pe: type of positional encoder. Available types: 'exp1d', 'lin1d', 'exp2d', 'lin2d', 'sincos', 'gauss' or 'normal', 'uniform', 'zeros', None.
            learn_pe: learned positional encoder (True, default) or fixed positional encoder.
            flatten: this will flatten the encoder output to be able to apply an mlp type of head (default=True)
            fc_dropout: dropout applied to the final fully connected layer.
            concat_pool: indicates whether global adaptive concat pooling will be used instead of global adaptive pooling.
            bn: indicates if batchnorm will be applied to the head.
            custom_head: custom head that will be applied to the network. It must contain all kwargs (pass a partial function)
            y_range: range of possible y values (used in regression tasks).
            kwargs: nn.Conv1d kwargs. If not {}, a nn.Conv1d with those kwargs will be applied to original time series.

        Input shape:
            x: bs (batch size) x nvars (aka features, variables, dimensions, channels) x seq_len (aka time steps)
            attn_mask: q_len x q_len
        """
        self.c_out, self.seq_len = c_out, seq_len

        # Input encoding
        q_len = seq_len
        self.new_q_len = False
        if max_seq_len is not None and seq_len > max_seq_len: # Control temporal resolution
            self.new_q_len = True
            q_len = max_seq_len
            tr_factor = math.ceil(seq_len / q_len)
            total_padding = (tr_factor * q_len - seq_len)
            padding = (total_padding // 2, total_padding - total_padding // 2)
            self.W_P = nn.Sequential(Pad1d(padding), Conv1d(c_in, d_model, kernel_size=tr_factor, stride=tr_factor))
            pv(f'temporal resolution modified: {seq_len} --> {q_len} time steps: kernel_size={tr_factor}, stride={tr_factor}, padding={padding}.\n', verbose)
        elif kwargs:
            self.new_q_len = True
            t = torch.rand(1, 1, seq_len)
            q_len = Conv1d(1, 1, **kwargs)(t).shape[-1]
            self.W_P = Conv1d(c_in, d_model, **kwargs) # Eq 2
            pv(f'Conv1d with kwargs={kwargs} applied to input to create input encodings\n', verbose)
        else:
            self.W_P = nn.Linear(c_in, d_model)        # Eq 1: projection of feature vectors onto a d-dim vector space

        # Positional encoding
        if pe == None:
            W_pos = torch.zeros((q_len, d_model), device=default_device()) # pe = None and learn_pe = False can be used to measure impact of pe
            learn_pe = False
        elif pe == 'zeros': W_pos = torch.zeros((q_len, d_model), device=default_device())
        elif pe == 'normal' or pe == 'gauss':
            W_pos = torch.zeros((q_len, d_model), device=default_device())
            torch.nn.init.normal_(W_pos, mean=0.0, std=0.1)
        elif pe == 'uniform':
            W_pos = torch.zeros((q_len, d_model), device=default_device())
            nn.init.uniform_(W_pos, a=0.0, b=0.1)
        elif pe == 'lin1d': W_pos = Coord1dPosEncoding(q_len, exponential=False, normalize=True)
        elif pe == 'exp1d': W_pos = Coord1dPosEncoding(q_len, exponential=True, normalize=True)
        elif pe == 'lin2d': W_pos = Coord2dPosEncoding(q_len, d_model, exponential=False, normalize=True)
        elif pe == 'exp2d': W_pos = Coord2dPosEncoding(q_len, d_model, exponential=True, normalize=True)
        elif pe == 'sincos': W_pos = SinCosPosEncoding(q_len, d_model, normalize=True)
        else: raise ValueError(f"{pe} is not a valid pe (positional encoder. Available types: 'gauss'=='normal', \
            'zeros', 'uniform', 'lin1d', 'exp1d', 'lin2d', 'exp2d', 'sincos', None.)")
        self.W_pos = nn.Parameter(W_pos, requires_grad=learn_pe)

        # Residual dropout
        self.res_dropout = nn.Dropout(res_dropout)

        # Encoder
        encoder_layer = TSTEncoderLayer(q_len, d_model, n_heads, d_k=d_k, d_v=d_v, d_ff=d_ff, res_dropout=res_dropout, activation=activation,
                                        res_attention=res_attention)
        self.encoder = TSTEncoder(encoder_layer, n_layers, res_attention=res_attention)
        self.transpose = Transpose(-1, -2, contiguous=True)

        # Head
        self.head_nf = d_model
        self.c_out = c_out
        self.seq_len = q_len
        if custom_head: self.head = custom_head(self.head_nf, c_out, q_len) # custom head passed as a partial func with all its kwargs
        else: self.head = self.create_head(self.head_nf, c_out, q_len, flatten=flatten, concat_pool=concat_pool, fc_dropout=fc_dropout, bn=bn, y_range=y_range)


    def create_head(self, nf, c_out, seq_len, flatten=True, concat_pool=False, fc_dropout=0., bn=False, y_range=None):
        if flatten:
            nf *= seq_len
            layers = [Flatten()]
        else:
            if concat_pool: nf *= 2
            layers = [GACP1d(1) if concat_pool else GAP1d(1)]
        layers += [LinBnDrop(nf, c_out, bn=bn, p=fc_dropout)]
        if y_range: layers += [SigmoidRange(*y_range)]
        return nn.Sequential(*layers)


    def forward(self, x:Tensor, attn_mask:Optional[Tensor]=None) -> Tensor:  # x: [bs x nvars x q_len], attn_mask: [q_len x q_len]

        # Input encoding
        if self.new_q_len: u = self.W_P(x).transpose(2,1) # Eq 2        # u: [bs x d_model x q_len] transposed to [bs x q_len x d_model]
        else: u = self.W_P(x.transpose(2,1))              # Eq 1        # u: [bs x q_len x d_model] transposed to [bs x q_len x d_model]

        # Positional encoding
        u = self.res_dropout(u + self.W_pos)

        # Encoder
        z = self.encoder(u, attn_mask=attn_mask)                        # z: [bs x q_len x d_model]
        z = self.transpose(z)                                           # z: [bs x d_model x q_len]

        # Classification/ Regression head
        return self.head(z)

    def show_pe(self, cmap='viridis', figsize=None):
        plt.figure(figsize=figsize)
        plt.pcolormesh(self.W_pos.detach().cpu(), cmap=cmap)
        plt.title('Positional Encoding')
        plt.colorbar()
        plt.show()

# Cell
@delegates(TSTPlus.__init__)
class MultiTSTPlus(Module):
    _arch = TSTPlus
    def __init__(self, feats, c_out, seq_len, max_seq_len:Optional[int]=512, custom_head=None, **kwargs):
        r"""
        MultiTST is a class that allows you to create a model with multiple branches of TST.

        Args:
            * feats: list with number of features that will be passed to each body.
        """
        self.feats = tuple(L(feats))
        self.kwargs = kwargs

        # Backbone
        self.branches = nn.ModuleList()
        self.head_nf = 0
        for feat in self.feats:
            m = create_model(self._arch, c_in=feat, c_out=c_out, seq_len=seq_len, max_seq_len=max_seq_len, **kwargs)
            self.head_nf += m.head_nf
            m.head = Noop
            self.branches.append(m)

        # Head
        self.c_out = c_out
        q_len = min(seq_len, max_seq_len)
        self.seq_len = q_len
        if custom_head is None:
            self.head = self._arch.create_head(self, self.head_nf, c_out, q_len, **kwargs)
        else:
            self.head = custom_head(self.head_nf, c_out, q_len, **kwargs)

    def forward(self, x):
        x = torch.split(x, self.feats, dim=1)
        for i, branch in enumerate(self.branches):
            out = branch(x[i]) if i == 0 else torch.cat([out, branch(x[i])], dim=1)
        return self.head(out)