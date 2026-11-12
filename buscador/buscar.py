"""
Buscador de ofertas laborales de la zona.

Corre solo en GitHub Actions cada pocas horas:
  1. Junta avisos de varias fuentes (Computrabajo, Google Noticias, buscador web).
  2. Filtra para quedarse solo con ofertas reales de la zona y las ordena por
     rubro: con reglas propias, o con una IA si se configura el secreto IA_KEY.
  3. Guarda todo en data/ofertas.json, que es lo que lee la página.

Cada fuente está aislada: si una falla, las demás siguen funcionando.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

import requests
import yaml
from bs4 import BeautifulSoup

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
ARCHIVO_OFERTAS = DATA / "ofertas.json"
ARCHIVO_DESCARTADOS = DATA / "descartados.json"

AR = timezone(timedelta(hours=-3))
AHORA = datetime.now(AR)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "es-AR,es;q=0.9",
}

RUBROS = [
    "Comercio y ventas",
    "Administración y oficina",
    "Agro",
    "Salud",
    "Educación",
    "Gastronomía y hotelería",
    "Construcción y oficios",
    "Industria y producción",
    "Transporte y logística",
    "Bancos y finanzas",
    "Tecnología",
    "Cuidado de personas y limpieza",
    "Sector público",
    "Otros",
]

ESTADISTICAS: dict[str, dict[str, int | str]] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", texto)).strip()


def id_de(*partes: str) -> str:
    return hashlib.sha1("|".join(partes).encode()).hexdigest()[:16]


def get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=25, **kw)
    r.raise_for_status()
    return r


def registrar(fuente: str, encontrados: int, error: str | None = None) -> None:
    e = ESTADISTICAS.setdefault(fuente, {"encontrados": 0, "errores": 0})
    e["encontrados"] += encontrados
    if error:
        e["errores"] += 1
        e["ultimo_error"] = error[:200]


# ---------------------------------------------------------------- fuentes


def fecha_relativa(texto: str) -> str:
    """Convierte 'Hace 3 días', 'Ayer', 'Hace 5 horas' a fecha ISO."""
    t = normalizar(texto)
    dias = 0
    if "ayer" in t:
        dias = 1
    elif m := re.search(r"hace (\d+) dia", t):
        dias = int(m.group(1))
    elif "mas de 30" in t:
        dias = 31
    elif m := re.search(r"(\d{1,2}) de ([a-z]+)", t):
        meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                 "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        if m.group(2) in meses:
            mes = meses.index(m.group(2)) + 1
            anio = AHORA.year if mes <= AHORA.month else AHORA.year - 1
            try:
                return datetime(anio, mes, int(m.group(1)), tzinfo=AR).date().isoformat()
            except ValueError:
                pass
    return (AHORA - timedelta(days=dias)).date().isoformat()


def computrabajo(loc: dict) -> list[dict]:
    """Avisos publicados en Computrabajo para la ciudad."""
    avisos: dict[str, dict] = {}
    for pagina in (1, 2):
        url = f"https://ar.computrabajo.com/empleos-en-buenos-aires-en-{loc['slug']}"
        if pagina > 1:
            url += f"?p={pagina}"
        html = get(url).text
        soup = BeautifulSoup(html, "html.parser")
        nuevos = 0
        for a in soup.select('a[href*="/ofertas-de-trabajo/"]'):
            href = a.get("href", "").split("#")[0].split("?")[0]
            # La URL termina en "-en-<ciudad>-<ID>": así descartamos las
            # sugerencias de otras ciudades que el portal mezcla en la lista.
            if f"-en-{loc['slug']}-" not in href:
                continue
            titulo = a.get_text(" ", strip=True)
            if not titulo or href in avisos:
                continue
            bloque = a.find_parent("article") or a.parent
            lineas = [l.strip() for l in bloque.get_text("\n").split("\n") if l.strip()]
            empresa = ""
            for l in lineas:
                if l != titulo and not re.match(r"^\d[,.]\d$", l) and loc["nombre"].lower() not in l.lower():
                    empresa = l
                    break
            fecha_txt = next((l for l in lineas if re.search(r"hace|ayer|de [a-z]+$|30 d", l, re.I)), "")
            avisos[href] = {
                "fuente": "Computrabajo",
                "url": urllib.parse.urljoin("https://ar.computrabajo.com", href),
                "titulo": titulo,
                "empresa_portal": empresa if empresa != titulo else "",
                "texto": " · ".join(lineas[:8]),
                "fecha": fecha_relativa(fecha_txt) if fecha_txt else AHORA.date().isoformat(),
                "localidad": loc["nombre"],
            }
            nuevos += 1
        if nuevos == 0:
            break
        time.sleep(2)
    return list(avisos.values())


def consulta_texto(loc: dict, cfg: dict) -> str:
    palabras = " OR ".join(f'"{p}"' for p in cfg["palabras_busqueda"])
    return f'"{loc["nombre"]}" ({palabras})'


def leer_rss(xml: str) -> list[dict]:
    raiz = ElementTree.fromstring(xml)
    items = []
    for it in raiz.iter("item"):
        def campo(nombre: str) -> str:
            n = it.find(nombre)
            return (n.text or "").strip() if n is not None else ""
        fecha = AHORA
        if campo("pubDate"):
            try:
                fecha = parsedate_to_datetime(campo("pubDate")).astimezone(AR)
            except (TypeError, ValueError):
                pass
        descripcion = BeautifulSoup(campo("description"), "html.parser").get_text(" ", strip=True)
        items.append({"titulo": campo("title"), "url": campo("link"),
                      "texto": descripcion[:500], "fecha_dt": fecha})
    return items


def google_noticias(loc: dict, cfg: dict) -> list[dict]:
    """Notas de diarios y radios locales sobre búsquedas laborales."""
    q = consulta_texto(loc, cfg) + " when:30d"
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
           + "&hl=es-419&gl=AR&ceid=AR:es-419")
    salida = []
    for it in leer_rss(get(url).text):
        salida.append({
            "fuente": "Noticias locales",
            "url": it["url"],
            "titulo": it["titulo"],
            "texto": it["texto"],
            "fecha": it["fecha_dt"].date().isoformat(),
            "localidad": loc["nombre"],
        })
    return salida


def buscador_web(loc: dict, cfg: dict) -> list[dict]:
    """Resultados web (incluye publicaciones públicas indexadas de redes)."""
    consultas = [
        consulta_texto(loc, cfg),
        f'"{loc["nombre"]}" banco ("búsqueda laboral" OR "ingreso de personal" OR "busca personal")',
    ]
    salida = []
    for q in consultas:
        url = "https://www.bing.com/search?format=rss&setlang=es&cc=AR&q=" + urllib.parse.quote(q)
        for it in leer_rss(get(url).text):
            # Bing no siempre trae fecha real: si no hay, se asume hoy.
            salida.append({
                "fuente": "Web" if "facebook.com" not in it["url"] and "instagram.com" not in it["url"] else "Redes sociales",
                "url": it["url"],
                "titulo": it["titulo"],
                "texto": it["texto"],
                "fecha": it["fecha_dt"].date().isoformat(),
                "localidad": loc["nombre"],
            })
        time.sleep(2)
    return salida


# ---------------------------------------------------------------- IA (GitHub Models)

# IA opcional: cualquier servicio compatible con OpenAI (Groq, Gemini, OpenRouter...).
# Si no hay clave configurada, se usa el filtro por reglas de más abajo.
URL_IA = os.environ.get("IA_URL") or "https://api.groq.com/openai/v1/chat/completions"
MODELO = os.environ.get("IA_MODELO") or "llama-3.3-70b-versatile"

INSTRUCCIONES = f"""Sos el editor de una bolsa de trabajo de la zona de 25 de Mayo, Saladillo,
Roque Pérez y alrededores (provincia de Buenos Aires, Argentina).
Te paso una lista de avisos o resultados de búsqueda. Para cada uno decidí:

- "es_oferta": true SOLO si es una vacante de trabajo concreta y vigente, con lugar de
  trabajo en la ciudad indicada o sus localidades (no en otra provincia ni "25 de Mayo"
  de La Pampa/San Juan/otra). false si es: una persona que busca trabajo, un curso,
  una nota general sobre desempleo, una oferta 100% remota de todo el país sin relación
  con la zona, publicidad, o algo que no podés confirmar.
- "puesto": nombre corto del puesto (ej. "Cajero/a", "Peón rural", "Enfermero/a").
- "empresa": empresa o empleador si se sabe, si no "".
- "localidad": la ciudad donde es el trabajo, elegida de {{LOCALIDADES}} (normalmente la ciudad buscada).
- "rubro": uno de {json.dumps(RUBROS, ensure_ascii=False)}.
- "resumen": una frase corta, escrita por vos, con lo esencial (requisitos, jornada). Máx 25 palabras.

Respondé SOLO con JSON: {{"resultados": [{{"n": <número>, "es_oferta": ..., "puesto": ..., "empresa": ..., "localidad": ..., "rubro": ..., "resumen": ...}}]}}"""


LOCALIDADES: list[str] = []


def clasificar_lote(lote: list[dict]) -> dict[int, dict] | None:
    token = os.environ.get("IA_KEY")
    if not token:
        return None
    entrada = "\n\n".join(
        f"#{i} | Fuente: {a['fuente']} | Ciudad buscada: {a['localidad']}\n"
        f"Título: {a['titulo']}\nTexto: {a.get('texto', '')[:400]}"
        for i, a in enumerate(lote)
    )
    cuerpo = {
        "model": MODELO,
        "messages": [
            {"role": "system", "content": INSTRUCCIONES.replace("{LOCALIDADES}", json.dumps(LOCALIDADES, ensure_ascii=False))},
            {"role": "user", "content": entrada},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    for intento in range(3):
        try:
            r = requests.post(URL_IA, json=cuerpo, timeout=90, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            })
            if r.status_code == 429:
                espera = int(r.headers.get("retry-after", "30"))
                if espera > 120:
                    log(f"  límite diario de la IA alcanzado; se sigue en la próxima corrida")
                    return None
                log(f"  IA ocupada, espero {espera}s")
                time.sleep(espera)
                continue
            if r.status_code >= 400 or not r.text.strip():
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]!r}")
            try:
                contenido = r.json()["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError) as e:
                raise RuntimeError(f"respuesta inesperada HTTP {r.status_code}: {r.text[:300]!r}") from e
            m = re.search(r"\{.*\}", contenido, re.S)
            if not m:
                raise RuntimeError(f"la IA no devolvió JSON: {contenido[:200]!r}")
            datos = json.loads(m.group(0))
            return {int(x["n"]): x for x in datos.get("resultados", []) if "n" in x}
        except Exception as e:  # noqa: BLE001
            log(f"  error IA (intento {intento + 1}): {str(e)[:200]}")
            time.sleep(10)
    return None


# ---------------------------------------------------------------- filtro por reglas

PALABRAS_OFERTA = re.compile(
    r"busca(n|mos)? (personal|emplead|cajer|vendedor|mozo|chofer|peon|operari|administrativ|enfermer|docente|profesional|tecnic|ayudante|repartidor|encargad|recepcionista)"
    r"|se (necesita|busca|requiere|solicita)|busqueda laboral|oferta laboral|oferta de empleo|vacante"
    r"|convocatoria (laboral|para cubrir|a inscripcion)|ingreso de personal|incorpora|seleccion de personal|trabaja con nosotros"
    r"|empleos? en |puestos? de trabajo")
EXCLUIR = re.compile(
    r"\bbusco (trabajo|empleo)|me ofrezco|curso|capacitaci|taller de|desemple|despid|paro |huelga|cierre de"
    r"|tasa de|indec|estadistic|jubilad|subsidio|beca para estudiantes|perdid|extravi|robo|policial")
RUBROS_CLAVE = [
    ("Bancos y finanzas", r"banco|bancari|financier|seguros|contable|contador"),
    ("Salud", r"enfermer|medic|salud|hospital|clinica|farmac|kinesi|odontolog|psicolog"),
    ("Educación", r"docente|maestr|profesor|escuela|educaci|jardin"),
    ("Agro", r"agro|rural|campo|tambo|cosecha|ganader|agronom|tractor|feedlot|semilla|acopio"),
    ("Gastronomía y hotelería", r"mozo|moza|cociner|gastronom|restaurant|\\bbar\\b|parrill|panader|hotel|bacher"),
    ("Transporte y logística", r"chofer|camion|logistic|repart|deposito|transport|delivery"),
    ("Construcción y oficios", r"albanil|construcc|electricist|plomer|soldador|mecanic|carpinter|herrer|pintor|gasista|oficial"),
    ("Industria y producción", r"operari|produccion|fabrica|planta|industri|laboratorio|frigorif|elaborador"),
    ("Tecnología", r"programador|desarroll|sistemas|informatic|software|soporte tecnico|\\bit\\b"),
    ("Cuidado de personas y limpieza", r"cuidador|ninera|limpieza|domestic|acompanante terapeut|geriatr"),
    ("Sector público", r"municipal|municipio|concurso|provincia de buenos aires|ministerio|intendencia"),
    ("Administración y oficina", r"administrativ|recepcion|secretari|oficina|rrhh|recursos humanos|data entry"),
    ("Comercio y ventas", r"vendedor|ventas|comercial|cajer|repositor|atencion al cliente|preventista|supermercado|tienda|local"),
]


def rubro_por_reglas(texto: str) -> str:
    t = normalizar(texto)
    for rubro, patron in RUBROS_CLAVE:
        if re.search(patron, t):
            return rubro
    return "Otros"


def clasificar_por_reglas(a: dict, alias_de: dict[str, list[str]]) -> dict:
    """Decide sin IA. Computrabajo ya viene filtrado por ciudad; el resto
    tiene que mencionar la ciudad y tener palabras de oferta laboral."""
    titulo = re.sub(r"\s+-\s+[^-]{2,60}$", "", a["titulo"]).strip()  # saca " - Nombre del diario"
    texto = normalizar(f"{a['titulo']} {a.get('texto', '')}")
    if a["fuente"] == "Computrabajo":
        es_oferta = True
    else:
        menciona = any(normalizar(x) in texto for x in alias_de[a["localidad"]])
        es_oferta = menciona and bool(PALABRAS_OFERTA.search(texto)) and not EXCLUIR.search(texto)
    resumen = ""
    if a["fuente"] != "Computrabajo":
        resumen = re.sub(r"\s+", " ", a.get("texto", "")).strip()
        resumen = resumen[:160].rsplit(" ", 1)[0] + "…" if len(resumen) > 160 else resumen
    return {"es_oferta": es_oferta, "puesto": titulo, "empresa": "", "localidad": a["localidad"],
            "rubro": rubro_por_reglas(f"{titulo} {a.get('texto', '')}"), "resumen": resumen}


# ---------------------------------------------------------------- guardado


def cargar(path: Path, defecto):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return defecto


def clave_duplicado(o: dict) -> str:
    return normalizar(f"{o['puesto']} {o['empresa']} {o['localidad']}")


def main() -> int:
    cfg = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8"))
    DATA.mkdir(exist_ok=True)
    LOCALIDADES[:] = [l["nombre"] for l in cfg["localidades"]]

    previas = cargar(ARCHIVO_OFERTAS, {"ofertas": []})["ofertas"]
    descartados: list[str] = cargar(ARCHIVO_DESCARTADOS, [])
    ya_vistos = {o["id"] for o in previas} | set(descartados)

    # 1. Recolectar
    crudos: list[dict] = []
    for loc in cfg["localidades"]:
        for nombre, fn in (("Computrabajo", lambda l: computrabajo(l)),
                           ("Google Noticias", lambda l: google_noticias(l, cfg)),
                           ("Buscador web", lambda l: buscador_web(l, cfg))):
            try:
                res = fn(loc)
                registrar(nombre, len(res))
                log(f"{nombre:16} {loc['nombre']:16} {len(res):3} resultados")
                crudos.extend(res)
            except Exception as e:  # noqa: BLE001
                registrar(nombre, 0, f"{loc['nombre']}: {e}")
                log(f"{nombre:16} {loc['nombre']:16} ERROR {str(e)[:150]}")
            time.sleep(1)

    # 2. Quedarse con lo nuevo (por URL)
    nuevos: dict[str, dict] = {}
    for a in crudos:
        a["id"] = id_de(a["url"])
        if a["id"] not in ya_vistos and a["id"] not in nuevos:
            nuevos[a["id"]] = a
    pendientes = list(nuevos.values())
    log(f"\n{len(crudos)} resultados en total, {len(pendientes)} nuevos para revisar")

    # 3. Clasificar con IA en lotes
    aceptadas: list[dict] = []
    alias_de = {l["nombre"]: l["alias"] + [l["nombre"]] for l in cfg["localidades"]}
    usar_ia = bool(os.environ.get("IA_KEY"))
    log("Filtro: " + ("IA (" + MODELO + ")" if usar_ia else "reglas (sin IA configurada)"))
    for i in range(0, len(pendientes), 20):
        lote = pendientes[i:i + 20]
        res = clasificar_lote(lote) if usar_ia else None
        if res is None:
            if usar_ia:
                log("  IA no respondió: este lote se filtra por reglas")
            res = {n: clasificar_por_reglas(a, alias_de) for n, a in enumerate(lote)}
        for n, a in enumerate(lote):
            r = res.get(n)
            if not r:
                continue  # sin respuesta: se reintenta la próxima vez
            if not r.get("es_oferta"):
                descartados.append(a["id"])
                continue
            aceptadas.append({
                "id": a["id"],
                "puesto": (r.get("puesto") or a["titulo"]).strip()[:90],
                "empresa": (r.get("empresa") or a.get("empresa_portal") or "").strip()[:80],
                "localidad": r.get("localidad") if r.get("localidad") in LOCALIDADES else a["localidad"],
                "rubro": r.get("rubro") if r.get("rubro") in RUBROS else "Otros",
                "resumen": (r.get("resumen") or "").strip()[:220],
                "fuente": a["fuente"],
                "url": a["url"],
                "fecha": a["fecha"],
                "detectado": AHORA.date().isoformat(),
            })
        if usar_ia:
            time.sleep(4)
    log(f"{len(aceptadas)} ofertas nuevas aceptadas")

    # 4. Unir, quitar duplicados y vencidas
    limite = (AHORA - timedelta(days=cfg.get("dias_vigencia", 30))).date().isoformat()
    todas: dict[str, dict] = {}
    for o in sorted(previas + aceptadas, key=lambda o: o["fecha"]):
        if o["fecha"] < limite:
            continue
        clave = clave_duplicado(o)
        if clave in todas:
            existente = todas[clave]
            fuentes = set(existente.get("otras_fuentes", [])) | {o["url"]}
            fuentes.discard(existente["url"])
            existente["otras_fuentes"] = sorted(fuentes)
            continue
        todas[clave] = o
    ofertas = sorted(todas.values(), key=lambda o: (o["fecha"], o["detectado"]), reverse=True)

    salida = {
        "actualizado": AHORA.isoformat(timespec="minutes"),
        "localidades": [l["nombre"] for l in cfg["localidades"]],
        "rubros": RUBROS,
        "fuentes": ESTADISTICAS,
        "ofertas": ofertas,
    }
    ARCHIVO_OFERTAS.write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    ARCHIVO_DESCARTADOS.write_text(json.dumps(descartados[-5000:]), encoding="utf-8")
    log(f"\nTotal publicado: {len(ofertas)} ofertas")
    log(json.dumps(ESTADISTICAS, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
