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

## Empresa → administradores → personas

Flujo completo: `ficha_empresa.py "NOMBRE"` (o los flows de Flowsint, ver abajo).

### Ficha de empresa (identidad + actividad pública)

```bash
uv run resolver_empresa.py "BLINDITEX"          # nombre/NIF → candidatos NIF (validado), LEI, web probable; marca homonimias
uv run ficha_empresa.py "Iberdrola" --max 10    # resolver + BDNS, CNMV, TEU/TEJU, CENDOJ, PLACSP, TED, sanciones UE
uv run ted.py A95758389                         # contratos UE adjudicados (TED)
uv run placsp.py "IBERDROLA CLIENTES"           # licitaciones adjudicadas (PLACSP)
uv run sanciones_ue.py "NOMBRE"                 # lista consolidada de sanciones financieras UE
```

| Script | Fuente | Mecanismo | Observaciones |
|---|---|---|---|
| `resolver_empresa.py` | BDNS, GLEIF, TED, CNMV + DNS | `terceros` (NIF - NOMBRE), `api.gleif.org/api/v1/lei-records?filter[entity.legalName]`, TED `winner-name`, búsqueda CNMV; prueba `<nombre>.es/.com/...` | Valida DNI/NIE/CIF. Agrupa por NIF; LEI solo si coincide la denominación. Homonimias marcadas, nunca fusionadas. |
| `ted.py` | api.ted.europa.eu v3 | POST `/v3/notices/search` con `winner-identifier IN (...)` o `winner-name ~ "..."` | Sin clave. Solo contratos sobre umbral UE (eForms 2023+). El identificador del adjudicatario a veces es el del comprador. |
| `placsp.py` | contrataciondelestado.es | JSF/MyFaces con estado: GET portal → POST `_idcl=linkFormularioBusqueda` → POST con `texAdjudicatarioMAQ`; paginación «Siguiente >>» | Sin CAPTCHA. «Adjudicatario» busca por palabras, no por NIF. |
| `sanciones_ue.py` | Comisión Europea (FSF) | GET `xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw` (token público), caché 24 h (`OSINT_ES_CACHE`) | Coincidencia de nombre ≠ identidad. |
| `ficha_empresa.py` | todas | Encadena los scripts; NIF donde se admite, denominación donde no | Con homonimia no elige NIF (usa `--nif`). |

### Registro mercantil (BORME) → administradores y cargos

| Script | Fuente | Clave | Notas |
|---|---|---|---|
| `borme.py` | BOE datos abiertos: `datosabiertos/api/borme/sumario/AAAAMMDD` + `diario_borme/txt.php` | No | Sección Primera (actos inscritos). Caché SQLite (`BORME_DB`). `xml.php` no se usa: lo excluye robots.txt. |
| `apispain.py` | api.apispain.es (BORME, BOE, BDNS, PLACE, AEAT) | `APISPAIN_API_KEY` | Free: 100 pet./mes, 10/min; histórico desde 2024. Sin clave solo `health` y `organos`. Da NIF. |

```bash
uv run borme.py "MERCADONA SA" --exacta --json          # últimos 90 días (modo índice)
uv run borme.py "ACME" --provincia MADRID --dias 365     # lo más barato si se conoce la provincia
uv run borme.py --persona "Juan García López" --dias 30  # descarga todas las provincias del rango
uv run borme.py "ACME" --backend apispain                # vía apispain (NIF, histórico desde 2024)
```

Coste (pausa 0,5 s): índice ≈ 2 peticiones/día hábil (90 días ≈ 120 pet., 30 MB, 1 min); `--provincia` ≈ 1/día;
`--completo`/`--persona` ≈ 30/día (90 días ≈ 1.900 pet., ~20 min). Todo se cachea. «Administradores vigentes» es una
inferencia sobre el rango consultado; el BORME no publica DNI ni NIF (posibles homónimos).

### Personas: correos y LinkedIn

| Script | Qué hace | Claves |
|---|---|---|
| `correos.py "Nombre Apellido" dominio.es` | Candidatos por patrón (tildes, ñ, dos apellidos) puntuados con: patrón real de la empresa (correos de su web, commits públicos de GitHub del dominio, Hunter), MX, Gravatar. **Sin verificación SMTP.** | `HUNTER_API_KEY`, `GITHUB_TOKEN` (opcionales) |
| `apify_actores.py` | Cliente Apify v2 (`run-sync-get-dataset-items`): LinkedIn por nombre/URL, empleados de empresa, Google Search. Tope de gasto `APIFY_MAX_USD` (0,10 $). Actores configurables `APIFY_ACTOR_<CLAVE>`. | `APIFY_TOKEN` |
| `personas_comun.py` | Normalización de nombres españoles y BORME («APELLIDOS, NOMBRE»), crawl ligero de la web, extracción de correos. | — |

**Aviso:** los actores de LinkedIn rascan linkedin.com, cuyas condiciones lo prohíben; usarlos es decisión de quien
ejecuta la herramienta. El SearXNG local no sirve hoy para dorks (CAPTCHA en casi todos los motores; solo yandex y
seznam respetan `site:`).

## Enrichers de Flowsint (`../es-enrichers/`)

Se montan como `flowsint_enrichers.es` y reutilizan estos scripts (montados en `/opt/osint-es-scripts`).

| Enricher | Entrada → salida | Relaciones |
|---|---|---|
| `es_org_resolver_identidad` | Organization → Organization | completa nif/lei/web; homónimos POSIBLE_IDENTIDAD; WEB_PROBABLE |
| `es_org_to_registry_borme` | Organization → Organization | ficha registral en el propio nodo |
| `es_org_to_officers_borme` | Organization → Individual | ADM_UNICO, CONSEJERO, APODERADO, SOCIO_UNICO… (EX_* si consta cese) |
| `es_individual_to_companies_borme` | Individual → Organization | cargos en otras empresas (caro: usa PROVINCIA) |
| `es_org_to_subvenciones_bdns` | Organization → Document | RECIBE_SUBVENCION, CONCEDIDA_POR |
| `es_org_to_registros_cnmv` | Organization → Document | REGISTRO_CNMV |
| `es_org_to_edictos_boe` | Organization → Document | MENCIONADA_EN_EDICTO, PUBLICADO_POR |
| `es_org_to_sentencias_cendoj` | Organization → Document | MENCIONADA_EN_SENTENCIA (máx. 50) |
| `es_org_to_contratos_placsp` / `es_org_to_contratos_ted` | Organization → Document | ADJUDICATARIA, LICITADO_POR |
| `es_org_to_sanciones_ue` | Organization → Document | SANCIONADA / POSIBLE_COINCIDENCIA_SANCIONES |
| `es_org_to_asn` | Organization → ASN | sustituto sin clave de `org_to_asn` (PeeringDB + RIPEstat + Team Cymru) |
| `es_domain_to_web_emails` | Domain → Email | PUBLISHES_EMAIL |
| `es_individual_to_emails` | Individual → Email | PROBABLE_EMAIL (dominio del parámetro o del grafo) |
| `es_individual_to_linkedin` | Individual → SocialAccount | HAS_LINKEDIN (Apify) |
| `es_org_to_linkedin_employees` | Organization → Individual | WORKS_AT + HAS_LINKEDIN (Apify) |

Los enrichers de personas toman la empresa y el dominio de los vecinos del Individual en el grafo.

Flows (`../flows/`, importables con `python flows/build_flows.py --token <JWT>`):
«Ficha de empresa (España)», «Empresa → personas y huella digital» y «Persona vinculada: huella profesional».

## Fuentes analizadas y descartadas

| Fuente | Por qué no hay script |
|---|---|
| AEPD (resoluciones) | El WAF devuelve 503 a cualquier petición que no venga de un navegador real, incluida la llamada AJAX de Drupal (`/views/ajax?view_name=resoluciones&search_api_fulltext=`). Automatizarlo supondría saltarse la protección anti-bots. |
| OEPM (Localizador de marcas) | La búsqueda web lleva reCAPTCHA. Existe un servicio web SOAP oficial, pero hay que solicitar acceso: https://www.oepm.es/es/sobre-OEPM/servicios-al-ciudadano/servicios-gratuitos/Servicios-web-de-la-OEPM/acceso-a-servicios-web/index.html |
| Directorio de sociedades (Registradores) y Whois `.es` (dominios.es) | CAPTCHA en cada consulta. |
| LibreBORME / OpenMercantil / bormeapi | LibreBORME tras challenge de Cloudflare; OpenMercantil no resuelve DNS; bormeapi da timeout. |
| EUIPO eSearch (marcas UE) | `/copla/ctmsearch/json` responde vacío tras la protección anti-bots (F5); la API oficial requiere registro. |
| OpenSanctions | La API exige clave. |
| PLACSP datos abiertos ATOM | Ficheros de cientos de MB: para análisis masivo, no para consultar una empresa. |

## Uso responsable

Son datos públicos, pero las personas físicas que aparecen (edictos, sentencias,
beneficiarios) tienen derechos RGPD. Úsalo para verificar empresas y contrapartes,
no para perfilar a particulares, y respeta los avisos legales de cada fuente.
