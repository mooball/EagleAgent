from dotenv import load_dotenv
load_dotenv()
import requests
import os

api_key = os.environ.get("GOOGLE_API_KEY")
url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
res = requests.get(url).json()
for model in res.get('models', []):
    print(model['name'])
