"""
Session 3 homework helper.

Shows what the retriever actually returns for a question, with scores,
so you can see when RAG is about to answer from the wrong FAQ row.

    python src/test_retrieval.py
"""

import os

from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_huggingface import HuggingFaceEmbeddings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(ROOT, "faiss_index")

QUESTIONS = [
    "Do you have a javascript course?",
    "Can I pay in EMI?",
    "I have a Mac, can I use Power BI on it?",
    "Should I learn Power BI or Tableau?",
    "Do you provide internships?",
    "What is the refund policy?",
    "Who is the prime minister of India?",
]


def main() -> None:
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )
    db = FAISS.load_local(INDEX_PATH, embeddings, allow_dangerous_deserialization=True)

    for question in QUESTIONS:
        print("=" * 78)
        print(f"Q: {question}")
        results = db.similarity_search_with_score(question, k=4)
        for rank, (doc, distance) in enumerate(results, 1):
            similarity = 1 - distance  # COSINE distance -> similarity
            text = doc.page_content.replace("\n", " ")[:110]
            print(f"  {rank}. sim={similarity:.3f} | {text}")
        print()


if __name__ == "__main__":
    main()
