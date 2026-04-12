from dotenv import load_dotenv
load_dotenv()
from google import genai

client = genai.Client()
for model in client.models.list():
    print(model.name)
