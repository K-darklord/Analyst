"""Random Matrix Theory (RMT) spectral features.

Given a window of log-returns, compute the correlation matrix and extract the
spectral features used by both ORCA (trend detection) and omd_finance
(collapse detection):

  - eigenvalues (sorted descending)
  - effective_rank  (exponential entropy of normalized eigenvalues)
  - absorption_ratio (fraction of variance explained by top-k eigenvectors)
  - spectral_entropy (Shannon entropy of eigenvalue distribution)
  - lambda_max / lambda_mp_ratio (largest eigenvalue vs Marchenko-Pastur bound)
  - participation_ratio of the top eigenvector (market-mode concentration)

References
----------
- ORCA (arXiv:2604.17251): 127 spectral features for rally/crash classification
- omd_finance (arXiv:2607.19005): spectral collapse as crisis precursor
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _eigenvalues(corr: np.ndarray) -> np.ndarray:
    """Return eigenvalues of a symmetric correlation matrix, sorted descending."""
    # eigh returns ascending; flip to descending.
    eigvals = np.linalg.eigvalsh(corr)
    return np.sort(eigvals)[::-1]


def effective_rank(eigvals: np.ndarray) -> float:
    """Effective (participation) rank via exponential of spectral entropy.

    Ranges from 1 (all variance in one component) to N (uniform distribution).
    omd_finance calls this the "effective dimension" — a sharp drop signals
    that the market is collapsing onto a single dominant factor.
    """
    e = np.abs(eigvals)
    s = e.sum()
    if s == 0:
        return 1.0
    p = e / s
    p = p[p > 0]
    entropy = -np.sum(p * np.log(p))
    return float(np.exp(entropy))


def absorption_ratio(eigvals: np.ndarray, top_k: int = 3) -> float:
    """Fraction of total variance absorbed by the top-k eigenvectors.

    High absorption => risk is concentrated in a few common factors
    (the "market mode" dominates).  Used by ORCA as a risk-concentration
    feature; also the core of Kritzman & Li's "Absorption Ratio" paper.
    """
    total = eigvals.sum()
    if total == 0:
        return 0.0
    return float(np.sum(eigvals[:top_k]) / total)


def spectral_entropy(eigvals: np.ndarray) -> float:
    """Shannon entropy of the normalized eigenvalue distribution (nats)."""
    e = np.abs(eigvals)
    s = e.sum()
    if s == 0:
        return 0.0
    p = e / s
    p = p[p > 0]
    return float(-np.sum(p * np.log(p)))


def marchenko_pastur_bound(n_assets: int, n_obs: int) -> float:
    """Upper bound of the Marchenko-Pastur spectrum for an iid matrix.

    lambda_plus = (1 + sqrt(n_assets / n_obs))^2
    Eigenvalues above this bound are "anomalous" — they carry real signal
    rather than noise.  lambda_max / lambda_plus quantifies how much the
    market deviates from random.
    """
    q = n_assets / max(n_obs, 1)
    return float((1.0 + np.sqrt(q)) ** 2)


def participation_ratio(eigenvector: np.ndarray) -> float:
    """Participation ratio of an eigenvector: 1 / sum(|v_i|^4).

    PR close to 1 => the vector is concentrated on one asset.
    PR close to N => the vector is spread across all assets.
    For the market-mode eigenvector, a rising PR means the market factor
    is broadening; a falling PR means it's concentrating (stress signal).
    """
    v = np.asarray(eigenvector).ravel()
    denom = np.sum(v**4)
    if denom == 0:
        return 1.0
    return float(1.0 / denom)


def compute_spectral_features(
    returns_window: pd.DataFrame,
    top_k: int = 3,
) -> dict[str, float]:
    """Compute all spectral features for one window of returns.

    Parameters
    ----------
    returns_window : DataFrame of shape (T, N) — log-returns over the window.
    top_k : number of top eigenvectors for absorption ratio.

    Returns
    -------
    dict with keys:
        lambda_max, lambda_2, lambda_min,
        effective_rank, absorption_ratio, spectral_entropy,
        lambda_mp_ratio, market_pr
    """
    corr = returns_window.corr().values
    n_assets, n_obs = corr.shape[0], returns_window.shape[0]

    eigvals = _eigenvalues(corr)
    # Top eigenvector (corresponding to lambda_max).
    # Recompute eigvecs for the top vector only.
    full_vals, full_vecs = np.linalg.eigh(corr)
    top_idx = int(np.argmax(full_vals))
    market_vec = full_vecs[:, top_idx]

    mp_bound = marchenko_pastur_bound(n_assets, n_obs)

    return {
        "lambda_max": float(eigvals[0]),
        "lambda_2": float(eigvals[1]) if len(eigvals) > 1 else 0.0,
        "lambda_min": float(eigvals[-1]),
        "effective_rank": effective_rank(eigvals),
        "absorption_ratio": absorption_ratio(eigvals, top_k=top_k),
        "spectral_entropy": spectral_entropy(eigvals),
        "lambda_mp_ratio": float(eigvals[0] / mp_bound) if mp_bound > 0 else 0.0,
        "market_pr": participation_ratio(market_vec),
        "n_assets": float(n_assets),
    }


def rolling_spectral_features(
    returns: pd.DataFrame,
    window: int = 90,
    top_k: int = 3,
) -> pd.DataFrame:
    """Compute spectral features on a rolling window over the full history.

    Returns a DataFrame indexed by date with one column per feature.
    Used by the collapse detector (omd) and trend detector (ORCA).
    """
    feats: list[dict] = []
    idx: list[pd.Timestamp] = []
    for end in range(window, len(returns) + 1):
        sub = returns.iloc[end - window : end]
        if sub.isna().any().any():
            continue
        f = compute_spectral_features(sub, top_k=top_k)
        feats.append(f)
        idx.append(returns.index[end - 1])
    return pd.DataFrame(feats, index=pd.DatetimeIndex(idx))
