import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY not found in .env")

print("API key loaded successfully.")

client = genai.Client(api_key=api_key)

print("Sending request to Gemini...")

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Say exactly: Gemini is working!"
)

print("Response received:")
print(response.text)