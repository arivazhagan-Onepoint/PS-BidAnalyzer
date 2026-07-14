"""
List the Gemini models available to this project's API key.

Uses the shared client from analyzer.gemini_client so it reads the key from
credentials/gemini_credentials.json (the same way the analyzer does) rather
than relying on a GEMINI_API_KEY environment variable.

Run from the project root:

    python -m genai_list_models
"""
from analyzer.gemini_client import get_client

client = get_client()

print("Available Gemini Models:")
print("-" * 30)

# Iterate through all models available to this account
for model in client.models.list():
    # Filter for models that can actually generate responses.
    # The google-genai SDK exposes this as `supported_actions`.
    if "generateContent" in (model.supported_actions or []):
        print(f"• Name: {model.name}")
        print(f"  Description: {model.description}\n")
