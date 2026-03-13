import os
from dotenv import load_dotenv
load_dotenv()
from langchain_google_genai import GoogleGenerativeAIEmbeddings
for m in ['text-embedding-004', 'models/text-embedding-004']:
    try:
        em = GoogleGenerativeAIEmbeddings(model=m)
        res = em.embed_documents(["hello"])
        print(f"SUCCESS: {m}, len: {len(res[0])}")
    except Exception as e:
        print(f"FAILED {m}: {str(e)[:100]}")
