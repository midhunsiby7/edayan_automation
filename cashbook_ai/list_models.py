import os
from config import GEMINI_API_KEY
from google import genai

client = genai.Client(api_key=GEMINI_API_KEY)
models = client.models.list()
for model in models:
    print(model.name)
