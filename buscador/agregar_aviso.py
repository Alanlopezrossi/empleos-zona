"""
Agrega a la página un aviso cargado a mano (desde el botón del navegador o el
formulario). Lo dispara GitHub Actions cuando se abre un "issue" con la
etiqueta "aviso". La IA ordena los datos; si no responde, se usa el texto tal cual.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import buscar  # noqa: E402
import yaml  # noqa: E402

PROMPT = f"""Te paso un aviso de trabajo copiado de una red social o cargado por un comercio de la zona
de 25 de Mayo, Saladillo, Roque Pérez y alrededores (provincia de Buenos Aires).
Extraé los datos y respondé SOLO con JSON:
{{"es_oferta": true/false, "puesto": "...", "empresa": "...", "localidad": uno de {{LOCALIDADES}},
"rubro": uno de {json.dumps(buscar.RUBROS, ensure_ascii=False)},
"resumen": "frase corta con requisitos y cómo postularse, máx 30 palabras",
"contacto": "teléfono/WhatsApp/mail de la EMPRESA si figura, si no \\"\\""}}
es_oferta es false solo si claramente NO es una oferta de trabajo (ej. alguien que busca trabajo)."""


def main() -> int:
    titulo = os.environ.get("AVISO_TITULO", "").strip()
    cuerpo = os.environ.get("AVISO_CUERPO", "").strip()
    url_issue = os.environ.get("AVISO_ISSUE_URL", "")
    cfg = yaml.safe_load((buscar.RAIZ / "config.yaml").read_text(encoding="utf-8"))
    locs = [l["nombre"] for l in cfg["localidades"]]
    buscar.LOCALIDADES[:] = locs

    m = re.search(r"https?://\S+", cuerpo)
    link = m.group(0).rstrip(").,") if m else url_issue
    texto = re.sub(r"(?im)^\s*(link|enlace|url)\s*:.*$", "", cuerpo).strip()

    res = buscar.pedir_ia(PROMPT.replace("{LOCALIDADES}", json.dumps(locs, ensure_ascii=False)),
                          f"Título: {titulo}\nTexto: {texto[:2500]}")
    datos = res[0] if res else {}
    if datos and datos.get("es_oferta") is False:
        print("RESULTADO=descartado")
        return 0

    # Respaldo sin IA: ciudad por palabras clave, puesto = primera línea
    normal = buscar.normalizar(f"{titulo} {texto}")
    loc_reglas = next((l["nombre"] for l in cfg["localidades"]
                       if any(buscar.normalizar(a) in normal for a in l["alias"] + [l["nombre"]])), locs[0])
    primera = next((l for l in texto.splitlines() if l.strip()), titulo)
    resumen = datos.get("resumen") or re.sub(r"\s+", " ", texto)[:200]
    if datos.get("contacto") and datos["contacto"] not in resumen:
        resumen = f"{resumen} Contacto: {datos['contacto']}"

    oferta = {
        "id": buscar.id_de(link + texto[:200]),
        "puesto": (datos.get("puesto") or (titulo if titulo and titulo.lower() != "aviso" else primera)).strip()[:90],
        "empresa": (datos.get("empresa") or "").strip()[:80],
        "localidad": datos.get("localidad") if datos.get("localidad") in locs else loc_reglas,
        "rubro": datos.get("rubro") if datos.get("rubro") in buscar.RUBROS else buscar.rubro_por_reglas(f"{titulo} {texto}"),
        "resumen": resumen.strip()[:260],
        "fuente": "Redes sociales" if re.search(r"facebook\.com|instagram\.com|fb\.com|wa\.me|whatsapp", link) else "Cargado a mano",
        "url": link,
        "fecha": buscar.AHORA.date().isoformat(),
        "detectado": buscar.AHORA.date().isoformat(),
    }
    archivo = buscar.ARCHIVO_OFERTAS
    d = json.loads(archivo.read_text(encoding="utf-8"))
    if any(o["id"] == oferta["id"] for o in d["ofertas"]):
        print("RESULTADO=repetido")
        return 0
    d["ofertas"].insert(0, oferta)
    d["actualizado"] = buscar.AHORA.isoformat(timespec="minutes")
    archivo.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"RESULTADO=agregado|{oferta['puesto']} · {oferta['localidad']} · {oferta['rubro']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
