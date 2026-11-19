# Customer Service Chatbot (RAG)

A retrieval-augmented Q&A bot over an e-learning company's FAQ sheet.
Question → embed → find the 4 closest FAQ rows → answer using only those rows.

## Stack

- Python 3.11 · Streamlit · FAISS (cosine) · LangChain for retrieval only
- Embeddings: `sentence-transformers/all-MiniLM-L6-v2` (local, CPU, free)
- Generation: `google-genai` SDK → `gemini-3.5-flash` (free tier)

## Run

```powershell
uv venv --python 3.11 .venv
uv pip install -r requirements.txt

copy .env.example .env      # then paste your key in .env
# key: https://aistudio.google.com/apikey

.\.venv\Scripts\streamlit run src\main.py
```

Click **Create Knowledgebase** once (builds `faiss_index/`), then ask questions.

Cold start: the first run of the page loads an 80 MB embedding model in the
background (~10 s), and the first question after a restart takes ~15 s. Later
questions answer in 2–4 s. The page paints immediately regardless.

## Architecture

```
dataset.csv (76 FAQ records over 253 lines)
   └─ CSVLoader (cp1252) ──> 76 documents
        └─ MiniLM embeddings ──> FAISS index (cosine)
                                   │
question ──> embedding ──> top-4 rows ──> prompt ──> Gemini ──> answer + sources
```

Two guardrails, in this order:

1. **Similarity floor** (`-0.30`): if the best hit is worse than this, return the
   refusal without calling the LLM. Saves tokens and prevents answering from noise.
2. **Prompt grounding**: the prompt allows only the retrieved rows and demands an
   exact refusal string otherwise.

## Evaluation

| # | Question | Answer | Best sim | Latency |
|---|---|---|---|---|
| 1 | Can I pay in EMI? | No | −0.035 | 2.4 s |
| 2 | I have a Mac, can I use Power BI on it? | Windows-only, or VirtualBox | 0.548 | 3.6 s |
| 3 | Do you provide internships? | Yes, we provide virtual internships | 0.443 | 3.0 s |
| 4 | Do you have a javascript course? | *I don't know…* (correct refusal) | −0.107 | 3.5 s |
| 5 | Who is the prime minister of India? | *I don't know…* (floor caught it) | −0.726 | 0.0 s |

Rank-1 retrieval check, 7 in-domain + 1 off-topic control:

| Embedding model | Correct top-1 | Off-topic rejected | Notes |
|---|---|---|---|
| all-MiniLM-L6-v2 | 7/7 | yes (−0.73) | kept: cleaner gap between hit and miss |
| BAAI/bge-small-en-v1.5 | 7/7 | **no** (+0.56) | scored a miss as similar to a valid FAQ |

BGE was rejected because it rated an unanswerable question at +0.56 — it would
have produced a confident wrong answer.

## Problems found in the original course code (2023 era)

| Issue | Fix |
|---|---|
| `langchain.llms.GooglePalm` | PaLM API is retired; `gemini-2.5-flash` now returns 404 for new users too → `google-genai` + `gemini-3.5-flash` |
| `langchain.*` import paths | moved to `langchain_community.*` |
| `FAISS.load_local` | needs `allow_dangerous_deserialization=True` |
| `as_retriever(score_threshold=0.7)` | silently useless without `search_type`; FAISS returns distance, not similarity |
| `CSVLoader(file_path="dataset.csv")` | wrong folder; now an absolute path |
| CSV encoded Windows-1252 | loaded as UTF-8 → apostrophes corrupted inside the knowledge base. Fixed with `encoding="cp1252"` |
| `sentence-transformer== 2.2.2` | wrong package name, 2021 version |

## Known limitations

- One FAQ row = one chunk. Multi-part questions that span two rows retrieve only one.
- The similarity floor cannot separate "weak match" from "no match": valid rows
  score as low as −0.09 while the javascript miss scores −0.107. Refusal is
  therefore carried mainly by the prompt, which is weaker than a real threshold.
- No conversation memory: each question is independent.
- No streaming: the answer appears after the full response is generated.
- No reranker and no evaluation harness yet.
