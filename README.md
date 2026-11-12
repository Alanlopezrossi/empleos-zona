# Empleos en la zona

Página que junta automáticamente las ofertas laborales de 25 de Mayo, Saladillo, Roque Pérez y alrededores.

## Cómo funciona

Cinco veces por día, GitHub Actions:

1. Busca avisos en **Computrabajo**, **Google Noticias** (diarios y radios locales) y el **buscador web** (incluye publicaciones públicas de redes que estén indexadas, y búsquedas de bancos).
2. Le pasa los resultados a la **IA de GitHub (GitHub Models)**, que se queda solo con vacantes reales de la zona, descarta gente que busca trabajo, cursos y notas generales, y ordena cada aviso por rubro.
3. Guarda todo en `data/ofertas.json` y publica la página en GitHub Pages.

Los avisos duplicados (el mismo puesto en varias fuentes) se unifican, y los de más de 30 días se archivan solos.

## Cambiar ciudades o palabras de búsqueda

Editá `config.yaml` desde GitHub (ícono del lápiz) y guardá. La página se actualiza sola.

## Correrlo a mano

Pestaña **Actions** → **Actualizar ofertas** → **Run workflow**.
