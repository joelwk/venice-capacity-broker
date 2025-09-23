import requests
import json

BASE_URL = "https://api.venice.ai/api/v1"


def chat_completion(model, messages, api_key):
    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    data = {"model": model, "messages": messages}
    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response.json()


def generate_image(model, prompt, api_key):
    url = f"{BASE_URL}/image/generate"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    data = {"model": model, "prompt": prompt}
    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response.json()


def list_models(api_key):
    url = f"{BASE_URL}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(url, headers=headers)
    return response.json()


def get_api_key_usage(api_key):
    url = f"{BASE_URL}/api_keys/rate_limits"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(url, headers=headers)
    return response.json()


# Example usage
if __name__ == "__main__":
    api_key = 'VENICE_API_KEY'

    # Chat completion example
    model = "llama-3.3-70b"
    messages = [{"role": "user", "content": "Why is the sky blue?"}]
    chat_response = chat_completion(model, messages, api_key)
    print("Chat Response:", chat_response)

    # Image generation example
    image_model = "fluently-xl"
    image_prompt = "A beautiful sunset over a mountain range"
    image_response = generate_image(image_model, image_prompt, api_key)
    print("Image Response:", image_response)

    # List models example
    models_response = list_models(api_key)
    print("Models Response:", models_response)

    # API key usage stats
    usage_stats = get_api_key_usage(api_key)
    print("API Key Usage Stats:", usage_stats)
