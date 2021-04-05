# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/013_callback.MVP.ipynb (unless otherwise specified).

__all__ = ['create_subsequence_mask', 'create_variable_mask', 'create_future_mask', 'create_mask', 'MVP_Loss',
           'TSBERT_Loss', 'MVP', 'TSBERT']

# Cell
from fastai.callback.all import *
from ..imports import *
from ..utils import *
from ..models.utils import *
from ..models.layers import *

# Cell
from torch.distributions.beta import Beta

# Cell
def create_subsequence_mask(o, r=.15, lm=3, stateful=True, sync=False):
    if o.ndim == 2: o = o[None]
    n_masks, mask_dims, mask_len = o.shape
    if sync == 'random': sync = random.random() > .5
    dims = 1 if sync else mask_dims
    numels = n_masks * dims * mask_len
    pm = 1 / lm
    pu = np.clip(pm * (r / max(1e-6, 1 - r)), 1e-3, 1)
    a, b, proba_a, proba_b = ([1], [0], pu, pm) if random.random() > pm else ([0], [1], pm, pu)
    if stateful:
        max_len = max(1, 2 * math.ceil(numels // (1/pm + 1/pu)))
        for i in range(10):
            _dist_a = np.random.geometric(proba_a, max_len)
            _dist_b = np.random.geometric(proba_b, max_len)
            dist_a = _dist_a if i == 0 else np.concatenate((dist_a, _dist_a), axis=-1)
            dist_b = _dist_b if i == 0 else np.concatenate((dist_b, _dist_b), axis=-1)
            if (dist_a + dist_b).sum() >= numels:
                break
            else:
                max_len *= 2
        dist_len = np.argmax((dist_a + dist_b).cumsum() >= numels) + 1
        l = [a*ax + b*bx for (ax, bx) in zip(dist_a[:dist_len], dist_b[:dist_len])]
        _mask = list(itertools.chain.from_iterable(l))[:numels]
        mask = o.new(_mask).reshape(n_masks, dims, mask_len)
    else:
        mask = o.new(np.random.binomial(1, 1 - r, (n_masks, dims, mask_len))) # faster than torch.distributions.binomial.Binomial
    if sync: mask = mask.repeat(1, mask_dims, 1)
    return mask

def create_variable_mask(o, r=.15):
    n_masks, mask_dims, mask_len = o.shape
    _mask = np.ones((n_masks * mask_dims, mask_len))
    if int(mask_dims * r) > 0:
        n_masked_vars = int(n_masks * mask_dims * r)
        sel_dims = np.random.choice(n_masks * mask_dims, n_masked_vars, False)
        _mask[sel_dims] = 0
    mask = o.new(_mask).reshape(*o.shape)
    return mask

def create_future_mask(o, r=.15, sync=False):
    if o.ndim == 2: o = o[None]
    n_masks, mask_dims, mask_len = o.shape
    if sync == 'random': sync = random.random() > .5
    if sync:
        sel_steps = int(round(mask_len * r))
        _mask = np.ones((1, 1, mask_len))
        _mask[..., -sel_steps:] = 0
        mask = o.new(_mask).repeat(n_masks, mask_dims, 1)
    else:
        _mask = np.ones((n_masks, mask_dims, mask_len))
        for i in range(n_masks):
            for j in range(mask_dims):
                steps = int(np.random.uniform(0, 2*r*mask_len))
                if steps == 0: continue
                _mask[i, j, -steps:] = 0
    mask = o.new(_mask)
    return mask

# Cell
def create_mask(o,  r=.15, lm=3, stateful=True, sync=False, subsequence_mask=True, variable_mask=False, future_mask=False, custom_mask=None):
    if r <= 0 or r >=1:
        return torch.ones_like(o)
    if int(r * o.shape[1]) == 0:
        variable_mask = False
    if subsequence_mask and variable_mask:
        random_thr = 1/3 if sync == 'random' else 1/2
        if random.random() > random_thr:
            variable_mask = False
        else:
            subsequence_mask = False
    if custom_mask is not None:
        return custom_mask(o)
    elif future_mask:
        return create_future_mask(o, r=r)
    elif subsequence_mask:
        return create_subsequence_mask(o, r=r, lm=lm, stateful=stateful, sync=sync)
    elif variable_mask:
        return create_variable_mask(o, r=r)
    else:
        raise ValueError('You need to set subsequence_mask, variable_mask or future_mask to True or pass a custom mask.')

# Cell
class MVP_Loss(Module):
    def __init__(self, crit=None):
        self.crit = ifnone(crit, MSELossFlat())
        self.mask = slice(None)

    def forward(self, preds, target):
        return self.crit(preds[self.mask], target[self.mask])

TSBERT_Loss = MVP_Loss

# Cell
import matplotlib.colors as mcolors


class MVP(Callback):
    order = 60

    def __init__(self, r: float = .15, subsequence_mask: bool = True, lm: float = 3., stateful: bool = True, sync: bool = False, variable_mask: bool = False,
                 future_mask: bool = False, custom_mask: Optional = None, dropout: float = .1, crit: callable = None, weights_path:Optional[str]=None,
                 target_dir: str = './data/MVP', fname: str = 'model', save_best: bool = True, verbose: bool = False):
        r"""
        Callback used to perform the pretext task of reconstruct the original data after a binary mask has been applied.

        Args:
            r: proba of masking.
            subsequence_mask: apply a mask to random subsequences.
            lm: average mask len when using stateful (geometric) masking.
            stateful: geometric distribution is applied so that average mask length is lm.
            sync: all variables have the same masking.
            variable_mask: apply a mask to random variables. Only applicable to multivariate time series.
            future_mask: used to train a forecasting model.
            custom_mask: allows to pass any type of mask with input tensor and output tensor.
            dropout: dropout applied to the head of the model during pretraining.
            crit: loss function that will be used. If None MSELossFlat().
            weights_path: indicates the path to pretrained weights. This is useful when you want to continue training from a checkpoint. It will load the
                          pretrained weights to the model with the MVP head.
            target_dir : directory where trained model will be stored.
            fname : file name that will be used to save the pretrained model.
            save_best: saves best model weights
    """
        assert subsequence_mask or variable_mask or future_mask or custom_mask, \
            'you must set (subsequence_mask and/or variable_mask) or future_mask to True or use a custom_mask'
        if custom_mask is not None and (future_mask or subsequence_mask or variable_mask):
            warnings.warn("Only custom_mask will be used")
        elif future_mask and (subsequence_mask or variable_mask):
            warnings.warn("Only future_mask will be used")
        store_attr("subsequence_mask,variable_mask,future_mask,custom_mask,dropout,r,lm,stateful,sync,crit,weights_path,fname,save_best,verbose")
        self.PATH = Path(f'{target_dir}/{self.fname}')
        if not os.path.exists(self.PATH.parent):
            os.makedirs(self.PATH.parent)
        self.path_text = f"pretrained weights_path='{self.PATH}.pth'"

    def before_fit(self):
        self.run = not hasattr(self, "lr_finder") and not hasattr(self, "gather_preds")
        if 'SaveModelCallback' in [cb.__class__.__name__ for cb in self.learn.cbs]:
            self.save_best =  False # avoid saving if SaveModelCallback is being used
        if not(self.run): return

        # prepare to save best model
        self.best = float('inf')

        # modify loss for denoising task
        self.old_loss_func = self.learn.loss_func
        self.learn.loss_func = MVP_Loss(self.crit)
        self.learn.MVP = self

        # remove and store metrics
        self.learn.metrics = L([])

        # change head with conv layer (equivalent to linear layer applied to dim=1)
        assert hasattr(self.learn.model, "head"), "model must have a head attribute to be trained with MVP"
        self.learn.model.head = nn.Sequential(nn.Dropout(self.dropout),
                                              nn.Conv1d(self.learn.model.head_nf, self.learn.dls.vars, 1)
                                             ).to(self.learn.dls.device)
        if self.weights_path is not None:
            transfer_weights(learn.model, self.weights_path, device=self.learn.dls.device, exclude_head=False)

        with torch.no_grad():
            xb = torch.randn(2, self.learn.dls.vars, self.learn.dls.len).to(self.learn.dls.device)
            assert xb.shape == self.learn.model(xb).shape, 'the model cannot reproduce the input shape'

    def before_batch(self):
        self.learn.yb = (self.x,)
        mask = create_mask(self.x,  r=self.r, lm=self.lm, stateful=self.stateful, sync=self.sync, subsequence_mask=self.subsequence_mask,
                           variable_mask=self.variable_mask, future_mask=self.future_mask, custom_mask=self.custom_mask)
        self.learn.xb = (self.x * mask,)
        self.learn.loss_func.mask = (mask == 0)  # boolean mask
        self.mask = mask

    def after_epoch(self):
        val = self.learn.recorder.values[-1][-1]
        if self.save_best:
            if np.less(val, self.best):
                self.best = val
                self.best_epoch = self.epoch
                torch.save(self.learn.model.state_dict(), f'{self.PATH}.pth')
                pv(f"best epoch: {self.best_epoch:3}  val_loss: {self.best:8.6f} - {self.path_text}", self.verbose or (self.epoch == self.n_epoch - 1))
            elif self.epoch == self.n_epoch - 1:
                print(f"\nepochs: {self.n_epoch} best epoch: {self.best_epoch:3}  val_loss: {self.best:8.6f} - {self.path_text}\n")

    def after_fit(self):
        self.run = True

    def show_preds(self, max_n=9, nrows=3, ncols=3, figsize=None, sharex=True, **kwargs):
        b = self.learn.dls.valid.one_batch()
        self.learn._split(b)
        xb = self.xb[0].detach().cpu().numpy()
        bs, nvars, seq_len = xb.shape
        self.learn('before_batch')
        pred = self.learn.model(*self.learn.xb).detach().cpu().numpy()
        mask = self.mask.cpu().numpy()
        masked_pred = np.ma.masked_where(mask, pred)
        ncols = min(ncols, math.ceil(bs / ncols))
        nrows = min(nrows, math.ceil(bs / ncols))
        max_n = min(max_n, bs, nrows*ncols)
        if figsize is None:
            figsize = (ncols*6, math.ceil(max_n/ncols)*4)
        fig, ax = plt.subplots(nrows=nrows, ncols=ncols,
                               figsize=figsize, sharex=sharex, **kwargs)
        idxs = np.random.permutation(np.arange(bs))
        colors = list(mcolors.TABLEAU_COLORS.keys()) + \
            random_shuffle(list(mcolors.CSS4_COLORS.keys()))
        i = 0
        for row in ax:
            for col in row:
                color_iter = iter(colors)
                for j in range(nvars):
                    try:
                        color = next(color_iter)
                    except:
                        color_iter = iter(colors)
                        color = next(color_iter)
                    col.plot(xb[idxs[i]][j], alpha=.5, color=color)
                    col.plot(masked_pred[idxs[i]][j],
                             marker='o', markersize=4, linestyle='None', color=color)
                i += 1
        plt.tight_layout()
        plt.show()

TSBERT = MVP