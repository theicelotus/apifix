"""Perturb passages before rating them. Needs packages the project does not declare."""

from typing import TYPE_CHECKING

import cv2  # noqa: F401  (frame extraction for the video variant)
import nlpaug.augmenter.word as naw
import torch
from sklearn.metrics import cohen_kappa_score  # noqa: F401
from textattack.augmentation import Augmenter  # noqa: F401
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from .api import rate

try:
    import ujson as json
except ImportError:
    import json

if TYPE_CHECKING:
    import pandas as pd  # noqa: F401


def perturbed_rating(passage):
    torch.manual_seed(0)
    text = naw.SynonymAug().augment(passage)
    tone = SentimentIntensityAnalyzer().polarity_scores(text)
    return json.dumps({"rating": rate(text), "tone": tone})
