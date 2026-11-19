"""
RAG pipeline for the customer-service chatbot project.

Flow:
    CSV of FAQs -> documents -> embeddings -> FAISS index on disk
                                                |
    question -> embedding -> top-k nearest rows -> prompt -> Gemini -> answer

Retrieval uses LangChain (CSVLoader, FAISS, HuggingFace embeddings).
Generation uses Google's current google-genai SDK, because the old
langchain-google-genai client only speaks the retired PaLM-era API.
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from google import genai
from langchain_community.document_loaders.csv_loader import CSVLoader
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_huggingface import HuggingFaceEmbeddings

load_dotenv()

# Absolute paths so the app works from any working directory.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "dataset", "dataset.csv")
INDEX_PATH = os.path.join(ROOT, "faiss_index")

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LLM_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = "gemini-3.5-flash-lite"
TOP_K = 4

# Below this cosine similarity the retrieved rows are noise, so don't pay for a
# call. Kept deliberately low: valid FAQ rows score as low as -0.09 here.
SIMILARITY_FLOOR = -0.30

REFUSAL = "I don't know. Let me connect you to a human agent."


@lru_cache(maxsize=1)
def get_embeddings() -> HuggingFaceEmbeddings:
    """Load the ~80 MB model on first use, not at import time.

    Loading it at module import blocks Streamlit's first paint for ~25 s, which
    looks like a blank page while the script "runs".
    """
    return HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)


def create_vector_db() -> None:
    """Read the FAQ CSV, embed every row, save the index to ./faiss_index."""
    # The CSV is Windows-1252, not UTF-8. Without this, apostrophes arrive as
    # mojibake and that corruption ends up inside the answers.
    docs = CSVLoader(
        file_path=DATA_PATH, source_column="prompt", encoding="cp1252"
    ).load()
    db = FAISS.from_documents(
        docs, get_embeddings(), distance_strategy=DistanceStrategy.COSINE
    )
    db.save_local(INDEX_PATH)


@lru_cache(maxsize=1)
def _load_index() -> FAISS:
    if not os.path.isdir(INDEX_PATH):
        raise FileNotFoundError(
            "No knowledge base found. Click 'Create Knowledgebase' first."
        )
    return FAISS.load_local(
        INDEX_PATH, get_embeddings(), allow_dangerous_deserialization=True
    )


@lru_cache(maxsize=1)
def _client() -> "genai.Client":
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Copy .env.example to .env and add a key "
            "from https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def build_prompt(context: str, question: str) -> str:
    return f"""You are a customer support agent for an e-learning company.
Answer ONLY using the CONTEXT below. Reuse the wording of the "response" field
when possible. If the CONTEXT does not contain the answer, reply exactly:
"{REFUSAL}"
Never invent prices, policies or course details. Keep the answer under 4 sentences.

CONTEXT:
{context}

QUESTION: {question}
"""


def _generate(prompt: str) -> str:
    last_error: Exception | None = None
    for model in (LLM_MODEL, FALLBACK_MODEL):
        try:
            response = _client().models.generate_content(model=model, contents=prompt)
            return (response.text or "").strip()
        except Exception as exc:  # 503s are common on the newest flash models
            last_error = exc
    raise RuntimeError(f"Gemini call failed: {last_error}")


def retrieve(question: str, k: int = TOP_K):
    """Return [(document, similarity)] best-first. Cosine distance -> similarity."""
    hits = _load_index().similarity_search_with_score(question, k=k)
    return [(doc, 1.0 - distance) for doc, distance in hits]


def ask(question: str) -> dict:
    hits = retrieve(question)
    best = hits[0][1] if hits else -1.0

    if best < SIMILARITY_FLOOR:
        return {"answer": REFUSAL, "sources": [], "scores": [], "best": best}

    context = "\n\n".join(doc.page_content for doc, _ in hits)
    answer = _generate(build_prompt(context, question))

    return {
        "answer": answer or REFUSAL,
        "sources": [doc.page_content.replace("\n", " ")[:220] for doc, _ in hits],
        "scores": [round(score, 3) for _, score in hits],
        "best": round(best, 3),
    }


if __name__ == "__main__":
    create_vector_db()
    print(f"Index built at {INDEX_PATH}")
