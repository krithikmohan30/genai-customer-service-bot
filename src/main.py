import streamlit as st

from langchain_helper import ask, create_vector_db


def fmt_score(value) -> str:
    """Format a similarity for display, whatever numeric type it arrives as."""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


st.set_page_config(page_title="Customer Service Chatbot", page_icon="🤖")
st.title("Customer Service Chatbot")

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.caption("Step 1: build the index once. Step 2: ask questions.")
    if st.button("Create Knowledgebase", use_container_width=True):
        with st.spinner(
            "Embedding 253 FAQ rows... the first run loads an 80 MB model (~25s)"
        ):
            try:
                create_vector_db()
                st.success("Knowledge base created.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message.get("sources"):
            with st.expander(
                f"Sources (best similarity {fmt_score(message.get('best'))})"
            ):
                for source, score in zip(message["sources"], message.get("scores", [])):
                    st.caption(f"sim={fmt_score(score)} · {source}")

question = st.chat_input("Ask about courses, pricing, internships...")

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        try:
            with st.spinner(
                "Working... the first question loads the 80 MB model (up to ~40s)"
            ):
                result = ask(question)
            answer = result["answer"]
            st.write(answer)
            if result["sources"]:
                with st.expander(
                    f"Sources (best similarity {fmt_score(result['best'])})"
                ):
                    for source, score in zip(result["sources"], result["scores"]):
                        st.caption(f"sim={fmt_score(score)} · {source}")
        except Exception as exc:
            answer = f"{type(exc).__name__}: {exc}"
            result = {"sources": [], "scores": [], "best": None}
            st.error(answer)

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer,
            "sources": result["sources"],
            "scores": result["scores"],
            "best": result["best"],
        }
    )
