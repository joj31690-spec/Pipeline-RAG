import streamlit as st
import requests

# Configuración de página
st.set_page_config(
    page_title="Asistente Normativo ASFI | Banco Unión",
    page_icon="🏛️",
    layout="wide"
)

API_URL = "http://backend:8000/api/consultar"
COLECCIONES_URL = "http://backend:8000/api/colecciones"
COLECCION_POR_DEFECTO = "asfi_bancaria_2026"

# Barra lateral con controles
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Escudo_de_Bolivia.svg/200px-Escudo_de_Bolivia.svg.png", width=70)
    st.title("⚖️ Control Normativo")
    st.markdown("Sistema RAG para resoluciones **ASFI** y normativa de **Banco Unión**.")
    st.divider()

    st.subheader("Colección Temática (aislamiento)")
    try:
        r = requests.get(COLECCIONES_URL, timeout=10)
        opciones = [c["coleccion_id"] for c in r.json().get("colecciones", [])] if r.ok else []
    except Exception:
        opciones = []
    if not opciones:
        opciones = [COLECCION_POR_DEFECTO]
    if COLECCION_POR_DEFECTO not in opciones:
        opciones.insert(0, COLECCION_POR_DEFECTO)
    coleccion_id = st.selectbox("Dominio consultado (coleccion_id):", opciones)

    st.subheader("Configuración de Recuperación")
    top_k = st.slider("Documentos a recuperar (top_k):", min_value=1, max_value=8, value=4)
    threshold = st.slider("Umbral mínimo de similitud:", min_value=0.20, max_value=0.80, value=0.35, step=0.05)
    
    st.divider()
    if st.button("Limpiar historial"):
        st.session_state.messages = []
        st.rerun()

st.title("🏛️ Asistente de Cumplimiento & Normativa ASFI")
st.caption("Consultas semánticas y vectoriales basadas en circulares oficiales y RNSF.")

# Inicializar sesión de chat
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Hola. Puedo asistirte en la interpretación y localización de resoluciones de la ASFI y circulares aplicadas a Banco Unión. ¿En qué puedo orientarte hoy?", "fuentes": []}
    ]

# Mostrar historial previo
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("fuentes"):
            with st.expander("📚 Fuentes y referencias normativas"):
                for idx, f in enumerate(msg["fuentes"], start=1):
                    st.markdown(f"**[{idx}] {f['documento']}** — *{f['articulo_ref']}* (Coincidencia: `{f['similitud']}%`)")
                    st.text(f['contenido'][:300] + ("..." if len(f['contenido']) > 300 else ""))

# Capturar consulta del usuario
if prompt := st.chat_input("Escribe tu consulta legal o normativa (ej. facultades de fiscalización, suficiencia patrimonial...)"):
    # Agregar mensaje de usuario
    st.session_state.messages.append({"role": "user", "content": prompt, "fuentes": []})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Llamar al backend FastAPI
    with st.chat_message("assistant"):
        with st.spinner("Consultando base vectorial y generando fundamentación jurídica..."):
            payload = {
                "pregunta": prompt,
                "coleccion_id": coleccion_id,
                "top_k": top_k,
                "match_threshold": threshold
            }
            try:
                response = requests.post(API_URL, json=payload, timeout=120)
                if response.status_code == 200:
                    data = response.json()
                    respuesta_texto = data.get("respuesta", "")
                    fuentes = data.get("fuentes", [])

                    st.markdown(respuesta_texto)
                    
                    if fuentes:
                        with st.expander("📚 Fuentes y referencias normativas"):
                            for idx, f in enumerate(fuentes, start=1):
                                st.markdown(f"**[{idx}] {f['documento']}** — *{f['articulo_ref']}* (Coincidencia: `{f['similitud']}%`)")
                                st.text(f['contenido'][:300] + ("..." if len(f['contenido']) > 300 else ""))

                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": respuesta_texto,
                        "fuentes": fuentes
                    })
                else:
                    err_msg = f"Error en el servidor API (Código {response.status_code}): {response.text}"
                    st.error(err_msg)
            except requests.exceptions.ConnectionError:
                st.error("No se pudo conectar con el servidor FastAPI (backend) en la red interna. Asegúrate de que el contenedor `lexbancario_backend` esté corriendo.")
            except Exception as e:
                st.error(f"Error inesperado: {e}")
