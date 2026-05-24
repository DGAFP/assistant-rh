# Application Streamlit (chatbot)
# Updated for monorepo structure - UI now in apps/streamlit-ui/
# PYTHONPATH=. ensures imports from src/ work when running from workspace root
web: PYTHONPATH=. streamlit run apps/streamlit-ui/Home.py --server.port $PORT --server.address 0.0.0.0
# API FastAPI (tests de charge) - décommenter pour activer l'API
# web: uvicorn api:app --host 0.0.0.0 --port $PORT
