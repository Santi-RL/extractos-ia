import json
import pandas as pd
import openai
import os
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader
import io
import yaml
import re

# Cargar la lista de bancos una sola vez al arrancar la app
with open("bancos.yml", encoding="utf-8") as f:
    BANCOS = yaml.safe_load(f)

def obtener_opciones_bancos():
    opciones = [("auto", "Detección automática de banco")]
    for banco in BANCOS:
        opciones.append((banco["id"], banco["nombre"]))
    return opciones

def detect_bank(texto, n=1000, buscar_todo=False):
    """
    Busca el banco en los primeros N caracteres del texto.
    Si no lo encuentra y buscar_todo=True, busca en todo el texto.
    Devuelve (banco_meta, modo_deteccion): 
    - banco_meta: dict de banco o dict 'desconocido'
    - modo_deteccion: 'inicio' o 'completo' o 'manual'
    """
    texto_inicio = texto[:n]
    for banco in BANCOS:
        for patron in banco["patrones"]:
            if re.search(patron, texto_inicio, re.I):
                return banco, "inicio"
    if buscar_todo:
        for banco in BANCOS:
            for patron in banco["patrones"]:
                if re.search(patron, texto, re.I):
                    return banco, "completo"
    # Si no encontró nada:
    return {
        "id": "desconocido",
        "nombre": "Desconocido",
        "fecha": "DD/MM/YYYY",
        "decimal": ","
    }, "no_detectado"

app = FastAPI()
templates = Jinja2Templates(directory="templates")

def extraer_texto_pdf(file_bytes):
    texto_paginas = []
    reader = PdfReader(io.BytesIO(file_bytes))
    for page in reader.pages:
        texto = page.extract_text()
        if texto:
            texto_paginas.append(texto)
    return "\n".join(texto_paginas)

def formatear_fecha(fecha, formato_original):
    from datetime import datetime
    try:
        if formato_original == "MM/DD/YYYY":
            dt = datetime.strptime(fecha, "%m/%d/%Y")
        elif formato_original == "DD/MM/YYYY":
            dt = datetime.strptime(fecha, "%d/%m/%Y")
        else:
            for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(fecha, fmt)
                    break
                except:
                    continue
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return fecha

def formatear_monto(x):
    try:
        x_str = str(x).replace(",", "")
        monto = float(x_str)
        return f"{monto:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return x

@app.get("/", response_class=HTMLResponse)
def form(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "bancos": obtener_opciones_bancos()
    })

@app.post("/accion", response_class=HTMLResponse)
async def accion(
    request: Request,
    file: UploadFile = File(...),
    accion: str = Form(...)
):
    file_bytes = await file.read()
    texto = extraer_texto_pdf(file_bytes)

    if accion == "extraer_texto":
        return templates.TemplateResponse("index.html", {
            "request": request,
            "texto_extraido": texto,
            "bancos": obtener_opciones_bancos()
        })

    elif accion == "detectar_banco":
        banco_meta, modo_deteccion = detect_bank(texto)
        return templates.TemplateResponse("index.html", {
            "request": request,
            "banco_detectado": banco_meta,
            "modo_deteccion": modo_deteccion,
            "bancos": obtener_opciones_bancos(),
            "file_bytes": file_bytes.hex()
        })

@app.post("/procesar", response_class=HTMLResponse)
async def procesar(
    request: Request,
    file_bytes: str = Form(...),
    banco_seleccionado: str = Form(...),
):
    from datetime import datetime

    file_bytes = bytes.fromhex(file_bytes)
    texto = extraer_texto_pdf(file_bytes)

    if banco_seleccionado != "auto":
        # El usuario eligió un banco manualmente
        banco_meta = next(
            (b for b in BANCOS if b["id"] == banco_seleccionado), {
                "id": "desconocido",
                "nombre": "Desconocido",
                "fecha": "DD/MM/YYYY",
                "decimal": ","
            }
        )
        modo_deteccion = "manual"
    else:
        # Detección automática (solo en los primeros 1000 caracteres)
        banco_meta, modo_deteccion = detect_bank(texto)

    if not texto:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "tabla": "<b>No se pudo extraer texto del PDF.</b>",
            "bancos": obtener_opciones_bancos()
        })

    # Prompt general por si el banco no tiene uno personalizado
    prompt_general = """
Analizá este extracto bancario y devolvé las transacciones en formato JSON.
Estructura:
- Date (formato DD/MM/YYYY)
- Description
- Amount (número puro con coma como decimal)
- Type (si es ingreso → "Crédito", si es egreso → "Débito")
Respondé SOLO el JSON con esta estructura:
{
  "transactions": [
    {
      "Date": "DD/MM/YYYY",
      "Description": "texto",
      "Amount": número,
      "Type": "Crédito/Débito"
    }
  ]
}
"""

    # Seleccioná el prompt: personalizado o general
    prompt = banco_meta.get("prompt", "").strip() if banco_meta.get("prompt") else ""
    if prompt:
        # Si hay prompt personalizado, agregá el texto del extracto al final
        prompt = f"{prompt}\nTexto del extracto:\n{texto}"
    else:
        # Si no hay personalizado, usá el general y agregá el texto del extracto al final
        prompt = f"{prompt_general}\nTexto del extracto:\n{texto}"

    # Configurar OpenAI
    import openai
    openai.api_key = os.getenv("OPENAI_API_KEY")

    try:
        response = openai.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": prompt}
            ]
        )
        output = response.choices[0].message.content

        json_str = output[output.find('{'):output.rfind('}')+1]
        data = json.loads(json_str)
        transacciones = data.get("transactions", [])
        df = pd.DataFrame(transacciones)

        if not df.empty:
            df["Date"] = df["Date"].apply(lambda f: formatear_fecha(f, banco_meta.get("fecha", "DD/MM/YYYY")))
            
            def normalizar_monto(row):
                try:
                    monto = float(str(row["Amount"]).replace(",", ".").replace(" ", ""))
                except Exception:
                    monto = 0.0
                tipo = str(row["Type"]).strip().lower().replace("é", "e").replace("í", "i")
                if tipo == "debito":
                    return -abs(monto)
                else:
                    return abs(monto)

            df["Amount"] = df.apply(normalizar_monto, axis=1)

            def formato_monto_argentino(monto):
                try:
                    return f"{monto:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                except Exception:
                    return monto
            df["Amount"] = df["Amount"].apply(formato_monto_argentino)
            
            columnas_es = {
                "Date": "Fecha",
                "Description": "Descripción",
                "Amount": "Importe",
                "Type": "Tipo"
            }
            df = df.rename(columns=columnas_es)
            tabla_html = df.to_html(classes="table table-striped", index=False)
        else:
            tabla_html = "<b>No se detectaron transacciones.</b>"

    except Exception as e:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "tabla": f"<b>Error procesando JSON o tabla: {e}</b>",
            "bancos": obtener_opciones_bancos()
        })

    return templates.TemplateResponse("index.html", {
        "request": request,
        "tabla": tabla_html,
        "bancos": obtener_opciones_bancos()
    })
