import google.generativeai as genai

genai.configure(api_key="dummy")


def upload(path: str):
    return genai.upload_file(path)


def legacy_chat(prompt: str) -> str:
    return genai.chat(messages=[prompt]).last
