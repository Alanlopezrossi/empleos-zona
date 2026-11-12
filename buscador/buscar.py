"""
Buscador de ofertas laborales de la zona.

Corre solo en GitHub Actions cada pocas horas:
  1. Junta avisos de varias fuentes (Computrabajo, Google Noticias, buscador web).
  2. Filtra para quedarse solo con ofertas reales de la zona y las ordena por
     rubro con una IA gratuita (LLM7 / Pollinations, sin registro); si no
     responde ninguna, con reglas propias.
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
    "Seguridad",
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


ETIQUETAS_PORTAL = re.compile(
    r"^(se precisa.*|postulado|empleo destacado|destacad[oa]|nuevo|urgente|vista|guardad[oa]|oferta oculta|ocultar|hace .*|ayer|"
    r"m[aá]s de 30 d[ií]as|\d{1,2} de [a-z]+|importante empresa del sector|empresa confidencial|valorar.*|\(.*\))$", re.I)


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
            # Mismo nombre existe en otras provincias (ej. 25 de Mayo, La Pampa):
            # exigimos que la ubicación diga "<Ciudad>, Buenos Aires" (no GBA/Capital).
            ubicacion_ok = any(
                normalizar(l).startswith(f"{normalizar(loc['nombre'])} buenos aires")
                and "gba" not in normalizar(l) for l in lineas)
            if not ubicacion_ok:
                continue
            empresa = ""
            for l in lineas:
                if l != titulo and not re.match(r"^\d[,.]\d$", l) and loc["nombre"].lower() not in l.lower() \
                        and not ETIQUETAS_PORTAL.match(l):
                    empresa = l
                    break
            link_empresa = bloque.select_one('a[href*="/empresas/"]') if hasattr(bloque, "select_one") else None
            if link_empresa and link_empresa.get_text(strip=True):
                empresa = link_empresa.get_text(" ", strip=True)
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
        # Publicaciones públicas de Facebook/Instagram que el buscador tenga indexadas
        f'(site:facebook.com OR site:instagram.com) "{loc["nombre"]}" ("busca personal" OR "se necesita" OR "se busca" OR "buscamos")',
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


# ---------------------------------------------------------------- IA

# Cadena de IAs gratuitas, en orden. Si una falla se prueba la siguiente, y si
# ninguna responde se usa el filtro por reglas. Ninguna requiere registrarse.
# (Opcional: con los secretos IA_KEY / IA_URL / IA_MODELO se agrega una propia al principio.)
PROVEEDORES = [
    {"nombre": "LLM7", "url": "https://api.llm7.io/v1/chat/completions",
     "clave": "unused", "modelo": os.environ.get("LLM7_MODELO") or "", "pausa": 3,
     "elegir_modelo": "https://api.llm7.io/v1/models"},
    {"nombre": "Pollinations", "url": "https://text.pollinations.ai/openai",
     "clave": None, "modelo": "openai", "pausa": 46},  # anónimo: 1 respuesta cada 45 s
]
if os.environ.get("IA_KEY"):
    PROVEEDORES.insert(0, {"nombre": "IA propia",
                           "url": os.environ.get("IA_URL") or "https://api.groq.com/openai/v1/chat/completions",
                           "clave": os.environ["IA_KEY"],
                           "modelo": os.environ.get("IA_MODELO") or "llama-3.3-70b-versatile", "pausa": 3})
FALLAS: dict[str, int] = {}
PREFERIDOS = ["gpt-4.1-mini", "gpt-4o-mini", "gpt-5-mini", "gpt-4.1-nano", "mistral-small", "flash",
              "deepseek", "llama", "qwen", "gemini", "8b", "default"]
INICIO = time.time()
MAX_SEGUNDOS_IA = 15 * 60  # después de esto, el resto se filtra por reglas


def elegir_modelo(prov: dict) -> None:
    """Pide la lista de modelos disponibles y elige uno (los gratuitos cambian seguido)."""
    url = prov.pop("elegir_modelo")
    try:
        datos = requests.get(url, headers={"Authorization": f"Bearer {prov['clave']}"}, timeout=30).json()
        ids = [m.get("id", "") for m in (datos.get("data", datos) if isinstance(datos, dict) else datos)
               if isinstance(m, dict)]
        log(f"  {prov['nombre']}: modelos disponibles: {', '.join(ids[:25])}")
        if prov["modelo"] in ids:
            return
        candidatos = []
        for pref in PREFERIDOS:
            candidatos += [i for i in ids if pref in i.lower() and i not in candidatos
                           and not re.search(r"audio|vision|voxtral|krea|chroma|image", i, re.I)]
        prov["candidatos"] = candidatos[:4] or ["default"]
        prov["modelo"] = prov["candidatos"].pop(0)
        log(f"  {prov['nombre']}: usando {prov['modelo']}")
    except Exception as e:  # noqa: BLE001
        prov["modelo"] = prov["modelo"] or "default"
        log(f"  {prov['nombre']}: no pude listar modelos ({str(e)[:100]})")


def pedir_ia(sistema: str, usuario: str) -> tuple[dict, str] | None:
    """Devuelve (json_respuesta, nombre_proveedor) o None si ninguna IA respondió."""
    for prov in PROVEEDORES:
        if FALLAS.get(prov["nombre"], 0) >= 3:
            continue  # falló demasiado en esta corrida: no insistir
        if prov.get("elegir_modelo"):
            elegir_modelo(prov)
        cuerpo = {"model": prov["modelo"], "temperature": 0.1, "max_tokens": 4000,
                  "messages": [{"role": "system", "content": sistema},
                               {"role": "user", "content": usuario}]}
        headers = {"Content-Type": "application/json"}
        if prov["clave"]:
            headers["Authorization"] = f"Bearer {prov['clave']}"
        for intento in range(3):
            if time.time() - INICIO > MAX_SEGUNDOS_IA:
                return None
            try:
                r = requests.post(prov["url"], json=cuerpo, headers=headers, timeout=120)
                if r.status_code in (401, 403) and prov.get("candidatos"):
                    log(f"  {prov['nombre']}: {prov['modelo']} no disponible sin clave, pruebo otro")
                    prov["modelo"] = prov["candidatos"].pop(0)
                    cuerpo["model"] = prov["modelo"]
                    continue
                if r.status_code in (401, 403):
                    FALLAS[prov["nombre"]] = 99
                    log(f"  {prov['nombre']}: requiere clave, se deja de usar")
                    break
                if r.status_code == 402:  # límite gratuito de Pollinations
                    m = re.search(r"Retry after (\d+)", r.text)
                    espera = min(int(m.group(1)) + 2 if m else 46, 60)
                    time.sleep(espera)
                    continue
                if r.status_code == 429:
                    espera = min(int(float(r.headers.get("retry-after", "20"))), 60)
                    log(f"  {prov['nombre']} ocupada, espero {espera}s")
                    time.sleep(espera)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]!r}")
                try:
                    contenido = r.json()["choices"][0]["message"]["content"] or ""
                except (ValueError, KeyError, IndexError, TypeError):
                    contenido = r.text  # algunos devuelven texto plano
                m = re.search(r"\{.*\}", contenido, re.S)
                if not m:
                    raise RuntimeError(f"no devolvió JSON: {contenido[:150]!r}")
                time.sleep(prov["pausa"])
                return json.loads(m.group(0)), prov["nombre"]
            except Exception as e:  # noqa: BLE001
                FALLAS[prov["nombre"]] = FALLAS.get(prov["nombre"], 0) + 1
                log(f"  {prov['nombre']} error: {str(e)[:200]}")
                time.sleep(5)
    return None


INSTRUCCIONES = f"""Sos el editor de una bolsa de trabajo de la zona de 25 de Mayo, Saladillo,
Roque Pérez y alrededores (provincia de Buenos Aires, Argentina).
Te paso una lista de avisos o resultados de búsqueda. Para cada uno decidí:

- "es_oferta": true SOLO si es una vacante de trabajo concreta y vigente, con lugar de
  trabajo en la ciudad indicada o sus localidades (no en otra provincia ni "25 de Mayo"
  de La Pampa/San Juan/otra). false si es: una persona que busca trabajo, un curso,
  una nota general sobre desempleo, una oferta 100% remota de todo el país sin relación
  con la zona, publicidad, o algo que no podés confirmar.
  OJO con los nombres repetidos: "25 de Mayo" debe ser la CIUDAD del partido de 25 de Mayo
  (provincia de Buenos Aires), NO la Avenida/calle 25 de Mayo de Capital Federal ni de ninguna
  ciudad, NI 25 de Mayo de La Pampa, San Juan o Misiones, ni el feriado. Lo mismo para
  "9 de Julio" (no la avenida de Capital), "General Alvear" (no Mendoza), "Lobos" y "Alberti".
- "puesto": nombre corto del puesto (ej. "Cajero/a", "Peón rural", "Enfermero/a").
- "empresa": empresa o empleador si se sabe, si no "".
- "localidad": la ciudad donde es el trabajo, elegida de {{LOCALIDADES}} (normalmente la ciudad buscada).
- "rubro": uno de {json.dumps(RUBROS, ensure_ascii=False)}.
- "resumen": una frase corta, escrita por vos, con lo esencial (requisitos, jornada). Máx 25 palabras.

Respondé SOLO con JSON: {{"resultados": [{{"n": <número>, "es_oferta": ..., "puesto": ..., "empresa": ..., "localidad": ..., "rubro": ..., "resumen": ...}}]}}"""


LOCALIDADES: list[str] = []


def clasificar_lote(lote: list[dict]) -> dict[int, dict] | None:
    entrada = "\n\n".join(
        f"#{i} | Fuente: {a['fuente']} | Ciudad buscada: {a['localidad']}\n"
        f"Título: {a['titulo']}\nTexto: {a.get('texto', '')[:400]}"
        for i, a in enumerate(lote)
    )
    sistema = INSTRUCCIONES.replace("{LOCALIDADES}", json.dumps(LOCALIDADES, ensure_ascii=False))
    res = pedir_ia(sistema, entrada)
    if res is None:
        return None
    datos, prov = res
    resultados = {int(x["n"]): x for x in datos.get("resultados", []) if isinstance(x, dict) and "n" in x}
    USO_IA[prov] = USO_IA.get(prov, 0) + len(resultados)
    return resultados


USO_IA: dict[str, int] = {}


# ---------------------------------------------------------------- verificación de zona


def zona_ok(texto: str, loc: dict) -> bool | None:
    """True = seguro que es la ciudad de la zona; False = es otro lugar
    (una avenida de Capital, otra provincia, el feriado...); None = no se sabe."""
    t = normalizar(texto)
    if any(re.search(n, t) for n in loc.get("negativas") or []):
        return False
    if not loc.get("ambiguo"):
        return True if any(normalizar(a) in t for a in loc["alias"] + [loc["nombre"]]) else None
    if any(re.search(pz, t) for pz in loc.get("positivas") or []):
        return True
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
    ("Salud", r"enfermer|medic|salud|hospital|clinica|farmac|kinesi|kineci|odontolog|psicolog"),
    ("Educación", r"docente|maestr|profesor|escuela|educaci|jardin"),
    ("Agro", r"agro|rural|campo|tambo|cosecha|ganader|agronom|tractor|feedlot|semilla|acopio"),
    ("Gastronomía y hotelería", r"mozo|moza|cociner|gastronom|restaurant|\bbar\b|parrill|panader|hotel|bacher"),
    ("Transporte y logística", r"chofer|camion|logistic|repart|deposito|transport|delivery|picking|polivalente"),
    ("Construcción y oficios", r"albanil|construcc|electricist|plomer|soldador|mecanic|carpinter|herrer|pintor|gasista|oficial"),
    ("Industria y producción", r"operari|produccion|fabrica|planta|industri|laboratorio|frigorif|elaborador|mantenimiento"),
    ("Tecnología", r"programador|desarroll|sistemas|informatic|software|soporte tecnico|\bit\b|\bqa\b|tester|erp|netsuite|shopify|ecommerce"),
    ("Cuidado de personas y limpieza", r"cuidador|ninera|limpieza|domestic|acompanante terapeut|geriatr"),
    ("Sector público", r"municipal|municipio|concurso|provincia de buenos aires|ministerio|intendencia"),
    ("Administración y oficina", r"administrativ|recepcion|secretari|oficina|rrhh|recursos humanos|data entry"),
    ("Comercio y ventas", r"vendedor|ventas|\bsales\b|comercial|comercios|cajer|repositor|relevador|asesor|atencion al cliente|preventista|supermercado|tienda|local"),
    ("Seguridad", r"vigilador|seguridad|custodia"),
    ("Construcción y oficios", r"tecnico"),
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
        es_oferta = (a.get("zona") is True and bool(PALABRAS_OFERTA.search(texto))
                     and not EXCLUIR.search(texto))
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

    urls_actuales = {a["url"] for a in crudos}
    ciudades_ct_ok = {a["localidad"] for a in crudos if a["fuente"] == "Computrabajo"}

    # 2. Quedarse con lo nuevo (por URL)
    nuevos: dict[str, dict] = {}
    for a in crudos:
        a["id"] = id_de(a["url"])
        if a["id"] not in ya_vistos and a["id"] not in nuevos:
            nuevos[a["id"]] = a
    loc_por_nombre = {l["nombre"]: l for l in cfg["localidades"]}
    pendientes = []
    fuera_de_zona = 0
    for a in nuevos.values():
        if a["fuente"] == "Computrabajo":
            a["zona"] = True  # ya verificado por la ubicación del portal
        else:
            a["zona"] = zona_ok(f"{a['titulo']} {a.get('texto', '')}", loc_por_nombre[a["localidad"]])
        # Ciudad con nombre ambiguo sin confirmar, o claramente otro lugar: afuera
        if a["zona"] is False or (a["zona"] is None and loc_por_nombre[a["localidad"]].get("ambiguo")):
            descartados.append(a["id"])
            fuera_de_zona += 1
            continue
        pendientes.append(a)
    log(f"{fuera_de_zona} descartados por no ser de la zona (calles, otras provincias, etc.)")
    log(f"\n{len(crudos)} resultados en total, {len(pendientes)} nuevos para revisar")

    # 3. Clasificar con IA en lotes
    aceptadas: list[dict] = []
    alias_de = {l["nombre"]: l["alias"] + [l["nombre"]] for l in cfg["localidades"]}
    usar_ia = True
    for i in range(0, len(pendientes), 10):
        lote = pendientes[i:i + 10]
        res = clasificar_lote(lote) if usar_ia else None
        if res is None:
            log("  ninguna IA respondió: este lote se filtra por reglas")
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
    log(f"Clasificados por IA: {USO_IA or 'ninguno'}")
    log(f"{len(aceptadas)} ofertas nuevas aceptadas")

    # 4. Unir, quitar duplicados y vencidas
    limite = (AHORA - timedelta(days=cfg.get("dias_vigencia", 30))).date().isoformat()
    todas: dict[str, dict] = {}
    for o in sorted(previas + aceptadas, key=lambda o: o["fecha"]):
        if o["fecha"] < limite:
            continue
        # Computrabajo: si el portal se leyó bien y el aviso ya no figura, se cubrió o venció
        if (o["fuente"] == "Computrabajo" and o["localidad"] in ciudades_ct_ok
                and o["url"] not in urls_actuales):
            continue
        # Revalidar avisos de diarios/web con las reglas de zona actuales
        if o["fuente"] in ("Noticias locales", "Web") and o["localidad"] in loc_por_nombre:
            z = zona_ok(f"{o['puesto']} {o.get('resumen', '')}", loc_por_nombre[o["localidad"]])
            if z is False or (z is None and loc_por_nombre[o["localidad"]].get("ambiguo")):
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
