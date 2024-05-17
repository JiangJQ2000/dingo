"""Generation of ligo skymaps from dingo results."""
import ligo.skymap.bayestar
from ligo.skymap.postprocess import crossmatch
from ligo.skymap import kde, io, moc, postprocess, plot
from typing import Optional
from bilby.gw.prior import UniformComovingVolume
from bilby.core.prior.analytical import PowerLaw

import numpy as np
from dingo.gw.result import Result


def generate_skymap_from_dingo_result(
    dingo_result: Result,
    num_samples: int = 5_000,
    num_trials: int = 1,
    num_jobs: int = 1,
    prior_distance_power: Optional[float] = None,
    cosmology: bool = False,
):
    """
    prior_distance_power : int, optional
        The power of distance that appears in the prior
        (default: 2, uniform in volume).
    cosmology: bool, optional
        Set to enable a uniform in comoving volume prior (default: false).
    """
    samples = dingo_result.samples
    if "weights" in samples:
        weights = np.array(samples["weights"])
    else:
        weights = np.ones(len(samples))

    # apply distance reweighting
    distance = np.array(samples["luminosity_distance"])
    prior_result = dingo_result.prior["luminosity_distance"]
    if cosmology:
        if prior_distance_power is not None:
            raise ValueError(
                "Only one of prior_distance_power and cosmology can be used."
            )
        prior_updated = UniformComovingVolume(
            prior_result.minimum, prior_result.maximum, name="luminosity_distance"
        )
        weights = weights * np.exp(
            prior_updated.ln_prob(distance) - prior_result.ln_prob(distance)
        )
        weights[np.where(np.isinf(prior_result.ln_prob(distance)))[0]] = 0
        weights /= weights.mean()
    elif prior_distance_power is not None:
        prior_updated = PowerLaw(
            prior_distance_power, prior_result.minimum, prior_result.maximum
        )
        weights = weights * np.exp(
            prior_updated.ln_prob(distance) - prior_result.ln_prob(distance)
        )
        weights[np.where(np.isinf(prior_result.ln_prob(distance)))[0]] = 0
        weights /= weights.mean()

    samples = samples.sample(num_samples, weights=weights, replace=True)
    ra_dec_dL = np.array(samples[["ra", "dec", "luminosity_distance"]])
    skypost = kde.Clustered2DSkyKDE(ra_dec_dL, trials=num_trials, jobs=num_jobs)
    skymap = skypost.as_healpix()

    return skymap