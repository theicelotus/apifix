import google.generativeai as genai

genai.configure(api_key="dummy")
model = genai.GenerativeModel("gemini-1.5-flash")
chat = model.start_chat(history=[])


def ask(message: str) -> str:
    text = ""
    for chunk in chat.send_message(message, stream=True):
        text += chunk.text
    return text


def history() -> list:
    return chat.history
