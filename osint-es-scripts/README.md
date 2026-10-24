# osint-es-scripts

Scripts de consulta a fuentes públicas españolas cuyos buscadores son solo web
(sin API documentada). Los endpoints se identificaron observando el tráfico de
cada web con Chrome en octubre de 2026; si una web cambia, el script se rompe.

Todos son autocontenidos (solo `httpx`) y se ejecutan con `uv`:

```bash
uv run bdns.py B26613679              # subvenciones recibidas (BDNS)
uv run cnmv.py Iberdrola              # entidades en los registros de la CNMV + ficha por NIF
uv run boe_edictos.py "NOMBRE S.L."   # TEU: notificaciones administrativas (BOE)
uv run boe_edictos.py "NOMBRE" --judicial   # TEJU: edictos judiciales
uv run cendoj.py "Nombre Empresa" --max 20  # jurisprudencia (CENDOJ)
```

Todos admiten `--json` para encadenarlos con otras herramientas o importarlos en Flowsint.

| Script | Fuente | Mecanismo | Observaciones |
|---|---|---|---|
| `bdns.py` | infosubvenciones.es | API JSON pública en dos pasos: `terceros` (NIF/nombre → id) y `concesiones/busqueda?beneficiario=<id>` | Sin clave. Tope 10.000 registros por consulta. Las concesiones a personas físicas se borran al año. |
| `cnmv.py` | cnmv.es | Búsqueda: POST ASP.NET con `__VIEWSTATE`. Ficha: GET `DatosEntidad?nif=` | Sin clave. Solo entidades con información en los Registros Oficiales (cotizadas, emisores, ESI, gestoras...). |
| `boe_edictos.py` | boe.es TEU / TEJU | GET con parámetros `campo[n]`/`dato[n]` | El texto del edicto solo es público 3 meses; después solo con CSV. |
| `cendoj.py` | poderjudicial.es | POST XHR a `search.action` con sesión previa | **Aviso legal del CENDOJ: prohibidos el uso comercial y la descarga masiva.** El script se limita a 200 resultados con pausa entre páginas. |

## Fuentes analizadas y descartadas

| Fuente | Por qué no hay script |
|---|---|
| AEPD (resoluciones) | El WAF devuelve 503 a cualquier petición que no venga de un navegador real, incluida la llamada AJAX de Drupal (`/views/ajax?view_name=resoluciones&search_api_fulltext=`). Automatizarlo supondría saltarse la protección anti-bots. |
| OEPM (Localizador de marcas) | La búsqueda web lleva reCAPTCHA. Existe un servicio web SOAP oficial, pero hay que solicitar acceso: https://www.oepm.es/es/sobre-OEPM/servicios-al-ciudadano/servicios-gratuitos/Servicios-web-de-la-OEPM/acceso-a-servicios-web/index.html |
| Directorio de sociedades (Registradores) y Whois `.es` (dominios.es) | CAPTCHA en cada consulta. |

## Uso responsable

Son datos públicos, pero las personas físicas que aparecen (edictos, sentencias,
beneficiarios) tienen derechos RGPD. Úsalo para verificar empresas y contrapartes,
no para perfilar a particulares, y respeta los avisos legales de cada fuente.
