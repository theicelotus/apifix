import os

import google.generativeai as genai


def get_gemini():
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    return genai.GenerativeModel("gemini-1.5-flash")


def ask(prompt):
    return get_gemini().generate_content(prompt).text
