import os
from dotenv import load_dotenv
load_dotenv()
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from config.settings import Config
for m in ['gemini-embedding-2-preview', 'text-embedding-005']:
    try:
        em = GoogleGenerativeAIEmbeddings(model=m, location=Config.EMBEDDINGS_LOCATION)
        res = em.embed_documents(["hello"])
        print(f"SUCCESS: {m}, len: {len(res[0])}")
    except Exception as e:
        print(f"FAILED {m}: {str(e)[:100]}")
