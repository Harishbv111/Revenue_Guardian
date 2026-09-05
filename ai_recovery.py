import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

def analyze_payment_with_gemini(amount, currency, failure_reason, recovery_probability):
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not configured")
    prompt = f"""
You are Revenue Guardian, an AI payment recovery agent.

Analyze this failed payment:
Amount: {amount} {currency}
Failure reason: {failure_reason}
Recovery probability: {recovery_probability:.0%}

Choose ONLY one action:
- RETRY_PAYMENT
- ALTERNATE_PAYMENT
- MANUAL_REVIEW

Return EXACTLY these four lines:
ACTION: <action>
REASON: <short reason>
PRIORITY: <HIGH/MEDIUM/LOW>
CUSTOMER_MESSAGE: <short customer-friendly message>

Rules: Do not mention internal systems or recovery probability. Do not invent details. Keep the message polite and simple. Do not blame the customer.
"""
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return response.text if response and response.text else ""
