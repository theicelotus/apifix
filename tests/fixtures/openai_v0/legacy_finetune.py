"""Kicks off fine-tuning jobs with both the legacy and the newer 0.x APIs."""

import openai


def start_legacy(training_file: str):
    return openai.FineTune.create(training_file=training_file, model="davinci")


def start_job(training_file: str):
    return openai.FineTuningJob.create(training_file=training_file, model="gpt-3.5-turbo")
