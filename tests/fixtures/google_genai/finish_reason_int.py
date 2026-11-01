import google.generativeai as genai

model = genai.GenerativeModel("gemini-1.5-flash")


def check(response):
    candidate = response.candidates[0]
    if candidate.finish_reason == 2:  # Safety
        return "blocked"
    if 3 != candidate.finish_reason:
        return "other"
    finish_reason = candidate.finish_reason
    if finish_reason in (3, 4):
        return "filtered"
    if finish_reason == genai.types.FinishReason.STOP:
        return "ok"
    return finish_reason == "STOP" or candidate.index == 2
