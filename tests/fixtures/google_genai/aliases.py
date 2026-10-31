import google.generativeai

from google import generativeai as gg

google.generativeai.configure(api_key="dummy")
model = gg.GenerativeModel("gemini-1.5-flash")
resp = model.generate_content("hi")
