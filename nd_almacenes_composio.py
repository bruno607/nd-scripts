"""
nd_almacenes_composio.py — CAMBIAR ALMACENES EN PEDIDOS, versión 100% COMPOSIO (v75, 02/10/2026; v83, 05/10/2026: relectura de FOs partida en documentos de <= 6 pedidos;
v88, 06/10/2026: cada bulk descargada debe ser LA PROPIA, firma de su query; si no, "CONTENIDO AJENO (...)" y exit 5;
v89, 06/10/2026: Q_PEDIDOS pide además `shippingLine { title } discountCodes` (prioridad Repo en engine.py) y el selftest
prueba también engine.py, en ../scripts/engine.py relativo a este script;
v90, 06/10/2026: el selftest corre además engine.py + diff.py de scripts/ con los casos de v90: FRAUDE/CONTRACARGO fuera
de scope, pedidos 100% despiece conservan RESERVA/SIN STOCK/SKU, y los 9 casos del criterio único de despiece;
v92, 06/10/2026: el selftest prueba además juntar_en_d10 de engine.py (pedidos que esperan algo en D10 juntan ahí sus líneas locales).

Sin credenciales, sin token propio, sin importar nd_shopify.py. Este script NO habla con Shopify:
solo procesa archivos. TODO el acceso a Shopify (lecturas, bulks, escrituras y relecturas de
verificación) lo hace Claude con la app de Composio para Shopify:
    SHOPIFY_BULK_QUERY_OPERATION     -> bulks (devuelve id, object_count, file_size y la URL firmada)
    SHOPIFY_GRAPH_QL_ADMIN_EXECUTE   -> escrituras (cada .graphql tal cual) y relecturas puntuales

Reglas de negocio: siguen en engine.py (motor), diff.py (tags/moves/splits) y build_changes_preview.py
(Excel PROPUESTA), SIN CAMBIOS (mismos hashes). Este script reemplaza a nd_almacenes_run.py, que queda
RETIRADO (no se borra del Proyecto; decide Northdeco).

Cambio frente a nd_almacenes_run.py: donde antes el script releía Shopify por su cuenta, ahora el script
IMPRIME el documento GraphQL de la relectura (en out/*.graphql), Claude lo ejecuta por Composio y escribe
la parte `data` de la respuesta en un JSON, y el script lo procesa. Las URL firmadas de las bulks viajan
en JSON (nunca en un comando).

FLUJO (orden obligatorio):
  0. Claude: ubicaciones (Q_LOCS) -> out/locations.json ; bulks en serie pedidos -> inventario -> out/bulks.json
  1. preparar   : descarga y valida, §2B, engine/diff/preview, lotes act_*/t1_*/t2_*, cola de FOs
  2. lotes act_* (Composio) -> verificar --hasta act --lectura out/lectura_act.json
  3. lotes t1_* (tagsRemove) -> bulk de tags -> verificar --hasta t1 --bulk out/tags_bulk.json
  4. lotes t2_* (tagsAdd)    -> bulk de tags -> verificar --hasta t2 --bulk out/tags_bulk.json
  5. bucle de FOs: `lote` (v77, hasta 15 pedidos por mutación; ver cmd_lote) o, de respaldo, `siguiente`
     (uno a uno): siguiente [--lectura out/fo_lectura.json] [--rechazo "..."] (ver cmd_siguiente)
  6. cerrar     : bulk final de pedidos -> cerrar --bulk out/final_bulk.json

USO
    python3 nd_almacenes_composio.py selftest
    python3 nd_almacenes_composio.py preparar --out DIR --scripts DIR_SCRIPTS --fecha DD-MM-AAAA \
        --bulks DIR/bulks.json --locations DIR/locations.json
    python3 nd_almacenes_composio.py verificar --out DIR --hasta act --lectura DIR/lectura_act.json
    python3 nd_almacenes_composio.py verificar --out DIR --hasta t1|t2 --bulk DIR/tags_bulk.json
    python3 nd_almacenes_composio.py lote --out DIR [--lectura DIR/fo_lectura.json] [--rechazo mN="texto" ...]
    python3 nd_almacenes_composio.py siguiente --out DIR [--lectura DIR/fo_lectura.json] [--rechazo "texto"]
    python3 nd_almacenes_composio.py cerrar --out DIR --bulk DIR/final_bulk.json

SALIDA: 0 ok · 1 ERROR · 2 uso · 3 DISCREPANCIAS / ABORTADO · 4 NO RELEÍDOS · 5 CONTENIDO AJENO (v88: la bulk
descargada no es la nuestra; relanzar ESA bulk UNA vez).
"""
import datetime
import hashlib
import json
import os
import pickle
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
D10_ALIASES = {"D10", "DHL"}  # regla transversal: DHL y D10 son el mismo almacen
TOPE_D10 = 25
LOTE_D10 = 16
LOTE_TAGS = 25
LOTE_FO = 15  # pasos de pedidos distintos por mutación en el bucle `lote` (v77)
# v83 (05/10/2026): una relectura de FOs cuesta ~157 puntos por pedido (fulfillmentOrders(first: 50) x
# lineItems(first: 100)) y Shopify rechaza toda consulta de más de 1.000 (MAX_COST_EXCEEDED, visto en shadow
# el 05/10 con 15 pedidos = 2.355). Toda relectura de FOs se parte en documentos de como mucho 6 pedidos
# (6 x 157 = 942); se ejecutan juntos en UN bloque (son lecturas) y sus `data` se guardan como LISTA.
MAX_PEDIDOS_LECTURA = 6

# Queries literales del maestro (§2.4). v89: Q_PEDIDOS + `shippingLine { title } discountCodes` (inline, sin objetos extra).
Q_PEDIDOS = """{ orders(query: "status:open") { edges { node { id name createdAt tags closed displayFinancialStatus displayFulfillmentStatus customer { displayName } sourceName totalPriceSet { shopMoney { amount currencyCode } } shippingLine { title } discountCodes fulfillmentOrders { edges { node { id status assignedLocation { name } deliveryMethod { methodType } lineItems { edges { node { id remainingQuantity lineItem { sku name } } } } } } } } } } }"""
Q_INVENTARIO = """{ productVariants { edges { node { id sku product { id status } inventoryItem { id inventoryLevels { edges { node { location { name } quantities(names: ["available","on_hand","incoming","committed"]) { name quantity } } } } } } } } }"""
Q_LOCS = "{ locations(first: 50, includeInactive: true){ nodes { id name isActive } } }"
Q_TAGS = '{ orders(query: "status:open") { edges { node { id tags } } } }'

FO_FRAG = ("... on Order { id name fulfillmentOrders(first: 50){ nodes { id status assignedLocation { name } "
           "lineItems(first: 100){ nodes { id remainingQuantity lineItem { sku } } } } } }")
ACT_FRAG = "... on InventoryItem { id inventoryLevels(first: 10){ nodes { location { name } } } }"


class NDError(Exception):
    pass


class BulkAjena(NDError):
    """v88: la bulk descargada es la de OTRO proceso (la app de Composio es compartida). Exit 5."""


# v88 (06/10/2026): firma de cada bulk propia, sacada de su query literal (§2.4 del maestro):
#   (tipo gid de las filas raíz, claves raíz que pide la query, claves raíz obligatorias,
#    tipos gid admitidos en las filas hijas, ¿se admiten filas hijas sin id?)
# Objetos inline (customer, totalPriceSet, product, inventoryItem, lineItem de la FO) van dentro de su fila.
FIRMAS = {
    # Q_PEDIDOS (también la bulk final de `cerrar`): fulfillmentOrders -> filas FulfillmentOrder (__parentId = Order);
    # sus lineItems -> filas FulfillmentOrderLineItem (__parentId = la FO). Obligatorias: las no anulables.
    # v89: shippingLine (puede ser null) y discountCodes permitidas, NO obligatorias.
    "pedidos": ("Order", {"id", "name", "createdAt", "tags", "closed", "displayFinancialStatus", "displayFulfillmentStatus",
                          "customer", "sourceName", "totalPriceSet", "shippingLine", "discountCodes"},
                {"id", "name", "createdAt", "tags", "closed", "displayFulfillmentStatus", "totalPriceSet"},
                {"FulfillmentOrder", "FulfillmentOrderLineItem"}, False),
    # Q_INVENTARIO: InventoryItem inline; los inventoryLevels son filas SIN id con __parentId = ProductVariant.
    "inventario": ("ProductVariant", {"id", "sku", "product", "inventoryItem"}, {"id", "sku", "product", "inventoryItem"},
                   set(), True),
    # Q_TAGS (verificar t1/t2): solo raíz, ninguna fila hija.
    "tags": ("Order", {"id", "tags"}, {"id", "tags"}, set(), False),
}


def comprobar_firma(path, bulk):
    """v88: no hace nada si `path` es la bulk propia `bulk` (vacía también vale); si no, BulkAjena."""
    raiz, piden, oblig, hijos, sin_id = FIRMAS[bulk]
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                r, motivo = None, "JSON roto"
            else:
                motivo = None if isinstance(r, dict) else "no es un objeto"
            if not motivo:
                i = r.get("id")
                tipo = i.split("/")[3] if isinstance(i, str) and i.startswith("gid://shopify/") else ""
                if "__parentId" not in r:
                    falta, sobra = sorted(oblig - set(r)), sorted(set(r) - piden)
                    if tipo != raiz or falta or sobra:
                        motivo = f"id {str(i)[:60]}, falta {falta}, sobra {sobra} (se espera raíz {raiz})"
                elif i is None and not sin_id:
                    motivo = f"fila hija sin id (se esperan {sorted(hijos) or 'ninguna'})"
                elif i is not None and tipo not in hijos:
                    motivo = f"id {str(i)[:60]}, hija de un tipo que la query no pide (se esperan {sorted(hijos) or 'ninguna'})"
            if motivo:
                raise BulkAjena(f"CONTENIDO AJENO ({bulk}): línea {n}, {motivo}. Probablemente es la bulk de OTRO proceso: "
                                f"NO seguir con ella; chequear currentBulkOperation y relanzar ESA bulk UNA vez (maestro §2.3).")


# ---------------------------------------------------------------------------
# Descarga y validación de una bulk (las URL las da Composio; nunca en un comando)
# ---------------------------------------------------------------------------

def _ssl_context():
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def contar_lineas(path):
    n = 0
    with open(path, "rb") as f:
        for linea in f:
            if linea.strip():
                n += 1
    return n


def validar_descarga(path, object_count, file_size, firma=None):
    esperado_obj = int(object_count or 0)
    lineas = contar_lineas(path) if os.path.exists(path) else 0
    if lineas != esperado_obj:
        raise NDError(f"Descarga {path}: {lineas} lineas != object_count {esperado_obj}.")
    if file_size not in (None, ""):
        b = os.path.getsize(path)
        if b != int(file_size):
            raise NDError(f"Descarga {path}: {b} bytes != file_size {file_size}.")
    if firma:
        comprobar_firma(path, firma)  # v88
    return {"lineas": lineas, "bytes": os.path.getsize(path)}


def bajar_bulk(meta, out_path, firma):
    """meta = {"id", "object_count", "file_size", "url"} (lo que devuelve Composio). firma = clave de FIRMAS (v88)."""
    for k in ("object_count", "url"):
        if k not in meta:
            raise NDError(f"Falta '{k}' en los datos de la bulk ({sorted(meta)}).")
    if int(meta.get("object_count") or 0) == 0 and not meta.get("url"):
        open(out_path, "wb").close()
        return validar_descarga(out_path, 0, None, firma)
    tmp = out_path + ".part"
    ultimo = None
    for intento in range(1, 5):
        try:
            req = urllib.request.Request(meta["url"].strip())
            with urllib.request.urlopen(req, timeout=600, context=_ssl_context()) as r, open(tmp, "wb") as f:
                while True:
                    blk = r.read(1 << 20)
                    if not blk:
                        break
                    f.write(blk)
            break
        except Exception as e:
            ultimo = e
            if intento == 4:
                raise NDError(f"Descarga de la bulk {meta.get('id')} fallo: {ultimo}.")
            time.sleep(2 ** intento)
    os.replace(tmp, out_path)
    return validar_descarga(out_path, meta["object_count"], meta.get("file_size"), firma)


def tags_de_jsonl(path):
    """{gid: [tags]} de una bulk de pedidos (cualquier selección que traiga id y tags)."""
    out = {}
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        o = json.loads(raw)
        if "__parentId" not in o and "tags" in o:
            out[o["id"]] = list(o.get("tags") or [])
    return out


# ---------------------------------------------------------------------------
# Tags (puro) — mismas funciones que nd_shopify.py, copiadas para no depender de él
# ---------------------------------------------------------------------------

def lotes(items, n):
    items = list(items)
    return [items[i:i + n] for i in range(0, len(items), n)]


def _norm(t):
    return (t or "").strip().upper()


def validar_plan_tags(plan):
    for oid, p in plan.items():
        choque = {_norm(t) for t in p.get("add", [])} & {_norm(t) for t in p.get("remove", [])}
        if choque:
            raise NDError(f"Plan contradictorio en {oid}: {sorted(choque)} en add y remove a la vez.")


def fases_tags(plan):
    validar_plan_tags(plan)
    f1 = [{"id": oid, "tags": list(p["remove"])} for oid, p in plan.items() if p.get("remove")]
    f2 = [{"id": oid, "tags": list(p["add"])} for oid, p in plan.items() if p.get("add")]
    return f1, f2


def diff_tags(esperado, real):
    e = {_norm(t): t for t in esperado}
    r = {_norm(t): t for t in real}
    return {"faltan": sorted(e[k] for k in e.keys() - r.keys()),
            "sobran": sorted(r[k] for k in r.keys() - e.keys())}


def verificar_tags(esperados, reales):
    """Devuelve (discrepancias, no_releidos). Un pedido ausente de `reales` NO es discrepancia: va aparte."""
    malos, ausentes = {}, []
    for oid, esp in esperados.items():
        if oid not in reales:
            ausentes.append(oid)
            continue
        d = diff_tags(esp, reales[oid])
        if d["faltan"] or d["sobran"]:
            malos[oid] = d
    return malos, ausentes


def estado_tras_fase(antes, plan, fase):
    out = {}
    for oid, p in plan.items():
        base = list(antes.get(oid, []))
        rm = {_norm(t) for t in p.get("remove", [])}
        tags = [t for t in base if _norm(t) not in rm]
        if fase == 2:
            ya = {_norm(t) for t in tags}
            tags += [t for t in p.get("add", []) if _norm(t) not in ya]
        out[oid] = tags
    return out


# ---------------------------------------------------------------------------
# §2B (puro)
# ---------------------------------------------------------------------------

def faltantes_d10(inventario_jsonl):
    variantes, niveles = {}, defaultdict(list)
    for raw in open(inventario_jsonl, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        o = json.loads(raw)
        if "__parentId" in o:
            niveles[o["__parentId"]].append((o.get("location") or {}).get("name"))
        else:
            variantes[o["id"]] = o
    out, universo = [], 0
    for vid, v in variantes.items():
        sku = (v.get("sku") or "").strip()
        if (v.get("product") or {}).get("status") != "ACTIVE" or not sku or sku.upper().startswith("PZ-"):
            continue
        universo += 1
        locs = [l for l in niveles.get(vid, []) if l]
        if not (set(locs) & D10_ALIASES):
            out.append({"sku": sku, "variant_id": vid, "inventory_item_id": (v.get("inventoryItem") or {}).get("id"),
                        "product_title": "", "variant_title": "", "locations": sorted(locs),
                        "falta_matadepera": "Matadepera" not in locs, "activado": ""})
    return out, universo


def d10_desde_ubicaciones(nodes):
    """(gid de D10 o None, {nombre canónico: gid}). D10 solo si hay EXACTAMENTE una Location D10/DHL."""
    hits = [n for n in nodes if n["name"] in D10_ALIASES]
    loc_ids = {_canon(n["name"]): n["id"] for n in nodes}
    return (hits[0]["id"] if len(hits) == 1 else None), loc_ids


# ---------------------------------------------------------------------------
# Plan de FOs (puro)
# ---------------------------------------------------------------------------

def _canon(n):
    return "D10" if n in D10_ALIASES else n


def estado_fos(results):
    out = {}
    for r in results:
        o = r["order"]
        fos = {}
        for fo in o["fos"]:
            if fo["status"] in ("CLOSED", "CANCELLED"):
                continue
            ls = {l["id"]: (l.get("sku"), l.get("remainingQuantity") or 0) for l in fo["lines"]
                  if (l.get("remainingQuantity") or 0) > 0}
            if ls:
                fos[fo["id"]] = {"loc": fo["location"], "lines": ls}
        out[o["id"]] = {"name": o["name"], "fos": fos}
    return out


def plan_fos(results, diff):
    by_name = {r["order"]["name"]: r for r in results}
    est = estado_fos(results)
    destino_linea = defaultdict(dict)
    for s in diff["splits"]:
        oid = by_name[s["order"]]["order"]["id"]
        for d in s["decisions"]:
            destino_linea[oid][d["line_id"]] = d["destino"]
    for m in diff["moves"]:
        oid = by_name[m["order"]]["order"]["id"]
        fo = est[oid]["fos"].get(m["fo_id"])
        if fo:
            for lid in fo["lines"]:
                destino_linea[oid][lid] = m["to"]
    plan = {}
    for oid, dl in destino_linea.items():
        e = est[oid]
        esperado = defaultdict(lambda: defaultdict(int))
        ops = []
        for fo_id, fo in e["fos"].items():
            grupos = defaultdict(list)
            for lid, (sku, q) in fo["lines"].items():
                dest = dl.get(lid, fo["loc"])
                if sku:
                    esperado[sku][_canon(dest)] += q
                grupos[dest].append({"id": lid, "quantity": q, "sku": sku})
            destinos = set(grupos)
            if destinos == {fo["loc"]}:
                continue
            if len(destinos) == 1:
                ops.append({"tipo": "move", "fo_id": fo_id, "from": fo["loc"], "to": next(iter(destinos))})
            else:
                queda = fo["loc"] if fo["loc"] in grupos else None
                for dest, lineas in grupos.items():
                    if dest == queda:
                        continue
                    ops.append({"tipo": "split_move", "fo_id": fo_id, "from": fo["loc"], "to": dest, "lineas": lineas})
                if queda is None:
                    ops[-1]["tipo"] = "move_resto"
        plan[oid] = {"name": e["name"], "ops": ops,
                     "esperado": {k: dict(v) for k, v in esperado.items()}}
    return plan


# ---------------------------------------------------------------------------
# Documentos GraphQL (literales, sin variables)
# ---------------------------------------------------------------------------

def _lit(v):
    return json.dumps(v, ensure_ascii=False)


def doc_activate(ops):
    partes = [f"m{i}: inventoryActivate(inventoryItemId: {_lit(o['inventoryItemId'])}, locationId: {_lit(o['locationId'])})"
              " { inventoryLevel { id } userErrors { field message } }" for i, o in enumerate(ops)]
    return "mutation { " + " ".join(partes) + " }"


def doc_tags(campo, ops):
    partes = [f"m{i}: {campo}(id: {_lit(o['id'])}, tags: {_lit(o['tags'])}) {{ userErrors {{ field message }} }}"
              for i, o in enumerate(ops)]
    return "mutation { " + " ".join(partes) + " }"


def _campo_split(fo_id, lineas):
    ls = ", ".join(f"{{id: {_lit(l['id'])}, quantity: {int(l['quantity'])}}}" for l in lineas)
    return ("fulfillmentOrderSplit(fulfillmentOrderSplits: [{fulfillmentOrderId: " + _lit(fo_id)
            + ", fulfillmentOrderLineItems: [" + ls + "]}]) { fulfillmentOrderSplits { remainingFulfillmentOrder { id } } "
            "userErrors { field message } }")


def _campo_move(fo_id, loc_id):
    return ("fulfillmentOrderMove(id: " + _lit(fo_id) + ", newLocationId: " + _lit(loc_id)
            + ") { movedFulfillmentOrder { id assignedLocation { name } } userErrors { field message } }")


def doc_split(fo_id, lineas):
    return "mutation { " + _campo_split(fo_id, lineas) + " }"


def doc_move(fo_id, loc_id):
    return "mutation { " + _campo_move(fo_id, loc_id) + " }"


def doc_lectura_fos(oids):
    partes = [f"n{i}: node(id: {_lit(o)}) {{ {FO_FRAG} }}" for i, o in enumerate(oids)]
    return "query { " + " ".join(partes) + " }"


def docs_lectura_fos(oids):
    """Relectura de FOs partida en documentos de como mucho MAX_PEDIDOS_LECTURA pedidos (v83)."""
    oids = list(oids)
    return [doc_lectura_fos(oids[i:i + MAX_PEDIDOS_LECTURA]) for i in range(0, len(oids), MAX_PEDIDOS_LECTURA)]


def _escribir_lecturas(path, docs):
    """out/fo_lectura.graphql: un documento por línea (v83)."""
    open(path, "w", encoding="utf-8").write("\n".join(docs) + "\n")


def _texto_lecturas(docs):
    n = len(docs)
    return "\n".join(f"=== RELECTURA {k}/{n} ===\n{doc}\n=== FIN ===" for k, doc in enumerate(docs, 1))


INSTR_LECTURA = ("Ejecutar los {n} documento(s) de RELECTURA con SHOPIFY_GRAPH_QL_ADMIN_EXECUTE, texto EXACTO, todos "
                 "juntos en UN bloque (son lecturas); si alguno falla, PARAR (las lecturas no tienen respaldo). "
                 "Escribir en {f} una LISTA JSON con la `data` de cada respuesta, en orden")


def doc_lectura_act(ids):
    return "query { nodes(ids: " + _lit(list(ids)) + ") { " + ACT_FRAG + " } }"


def lotes_hibridos(falt, d10, tags):
    out = []
    if falt and d10 and len(falt) <= TOPE_D10:
        ops = [{"inventoryItemId": v["inventory_item_id"], "locationId": d10} for v in falt]
        for n, l in enumerate(lotes(ops, LOTE_D10), 1):
            out.append({"nombre": f"act_{n:02d}", "bloque": "act", "campo": "inventoryActivate", "ops": l, "query": doc_activate(l)})
    f1, f2 = fases_tags(tags) if tags else ([], [])
    for clave, campo, ops in (("t1", "tagsRemove", f1), ("t2", "tagsAdd", f2)):
        for n, l in enumerate(lotes(ops, LOTE_TAGS), 1):
            out.append({"nombre": f"{clave}_{n:02d}", "bloque": clave, "campo": campo, "ops": l, "query": doc_tags(campo, l)})
    return out


# ---------------------------------------------------------------------------
# Bucle de FOs (puro)
# ---------------------------------------------------------------------------

def cola_fos(fos):
    return [{"oid": oid, "pedido": p["name"], "op": op} for oid, p in fos.items() for op in p["ops"]]


def _skus_fo(fo):
    return {s for s, _ in fo["lines"].values() if s}


def _buscar_fo(fos, loc, skus):
    for fid, fo in fos.items():
        if _canon(fo["loc"]) == _canon(loc) and set(skus) <= {s for s, _ in fo["lines"].values()}:
            return fid
    return None


def fos_de_nodo(n):
    """FOs abiertas de un nodo Order releído: {fo_id: {"loc", "lines": {line_id: (sku, qty)}}}."""
    out = {}
    for fo in n["fulfillmentOrders"]["nodes"]:
        if fo["status"] in ("CLOSED", "CANCELLED"):
            continue
        out[fo["id"]] = {"loc": (fo.get("assignedLocation") or {}).get("name"),
                         "lines": {l["id"]: ((l.get("lineItem") or {}).get("sku"), l.get("remainingQuantity") or 0)
                                   for l in fo["lineItems"]["nodes"] if (l.get("remainingQuantity") or 0) > 0}}
    return out


def nodos_lectura(obj):
    """{order_gid: nodo Order} a partir de la parte `data` de la respuesta (con o sin envoltorios)."""
    out = {}

    def walk(x):
        if isinstance(x, dict):
            if str(x.get("id", "")).startswith("gid://shopify/Order/") and "fulfillmentOrders" in x:
                out[x["id"]] = x
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return out


def paso_fo(st, fos_act, loc_ids):
    """Siguiente documento a ejecutar para st['cola'][st['i']]. Devuelve (pendiente, None) o (None, rechazo)."""
    it = st["cola"][st["i"]]
    op = it["op"]
    dest_id = loc_ids.get(_canon(op["to"]))
    if not dest_id:
        return None, "ubicación destino desconocida"
    skus_op = [l["sku"] for l in op.get("lineas", []) if l.get("sku")]
    if op["tipo"] == "split_move" and st.get("sub") != "move":
        fo_id = op["fo_id"] if op["fo_id"] in fos_act else _buscar_fo(fos_act, op["from"], skus_op)
        if not fo_id:
            return None, "FO de origen no encontrada (abierta, con esos SKUs)"
        lineas = []
        for l in op["lineas"]:
            lid = l["id"]
            if lid not in fos_act[fo_id]["lines"]:
                lid = next((x for x, (s, q) in fos_act[fo_id]["lines"].items() if s == l["sku"]), None)
            if lid:
                lineas.append({"id": lid, "quantity": l["quantity"]})
        if not lineas:
            return None, "ninguna línea a separar encontrada"
        return {"paso": "split", "fo_id": fo_id, "from": op["from"], "skus": skus_op,
                "query": doc_split(fo_id, lineas)}, None
    if op["tipo"] == "split_move":
        fo_id, skus = st["fo_sep"], skus_op
    elif op["tipo"] == "move_resto":
        fo_id = op["fo_id"] if op["fo_id"] in fos_act else _buscar_fo(fos_act, op["from"], skus_op)
        skus = skus_op
    else:
        fo_id = op["fo_id"] if op["fo_id"] in fos_act else None
        skus = sorted(_skus_fo(fos_act[fo_id])) if fo_id else []
    if not fo_id or fo_id not in fos_act:
        return None, "FO a mover no encontrada (abierta)"
    if _canon(fos_act[fo_id]["loc"]) != _canon(op["from"]):
        return None, f"la FO ya no está en {op['from']} (está en {fos_act[fo_id]['loc']})"
    return {"paso": "move", "fo_id": fo_id, "to": op["to"], "skus": skus, "query": doc_move(fo_id, dest_id)}, None


def confirmar_fo(st, fos_act):
    """Comprueba por relectura el paso pendiente. Devuelve None si cuadra o el motivo si no."""
    p = st["pendiente"]
    op = st["cola"][st["i"]]["op"]
    if p["paso"] == "split":
        want = set(p["skus"])
        cand = [f for f, fo in fos_act.items() if _canon(fo["loc"]) == _canon(p["from"]) and _skus_fo(fo) == want]
        if not cand:
            return "tras el split no aparece una FO en origen con exactamente esos SKUs"
        st["fo_sep"] = cand[0]
        st["sub"] = "move"
        return None
    en_destino = set().union(*[_skus_fo(fo) for fo in fos_act.values() if _canon(fo["loc"]) == _canon(p["to"])] or [set()])
    faltan = [s for s in p["skus"] if s not in en_destino]
    if faltan:
        return f"tras el move no están en {p['to']}: {faltan}"
    st["ok"].append({"pedido": st["cola"][st["i"]]["pedido"], "tipo": op["tipo"], "from": op["from"], "to": op["to"]})
    st["i"] += 1
    st["sub"] = None
    st.pop("fo_sep", None)
    return None


# ---------------------------------------------------------------------------
# Estado final desde una bulk de pedidos (puro)
# ---------------------------------------------------------------------------

def estado_jsonl(path):
    """Desde la bulk de pedidos: tags y ubicación por SKU (FOs abiertas) de cada pedido."""
    ords, fos, lines = {}, {}, []
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        o = json.loads(raw)
        tipo = o["id"].split("/")[3]
        if tipo == "Order":
            ords[o["id"]] = {"name": o["name"], "tags": o.get("tags") or [], "ubic": defaultdict(lambda: defaultdict(int))}
        elif tipo == "FulfillmentOrder":
            fos[o["id"]] = {"order": o["__parentId"], "status": o["status"],
                            "loc": (o.get("assignedLocation") or {}).get("name")}
        elif tipo == "FulfillmentOrderLineItem":
            lines.append(o)
    for l in lines:
        fo = fos.get(l["__parentId"])
        if not fo or fo["status"] in ("CLOSED", "CANCELLED") or not (l.get("remainingQuantity") or 0):
            continue
        sku = (l.get("lineItem") or {}).get("sku")
        if sku and fo["order"] in ords:
            ords[fo["order"]]["ubic"][sku][_canon(fo["loc"])] += l["remainingQuantity"]
    for o in ords.values():
        o["ubic"] = {k: dict(v) for k, v in o["ubic"].items()}
    return ords


def comparar_ubicaciones(esperado, post):
    """esperado = {order_gid: {sku: {ubicacion: cantidad}}}; post = estado_jsonl. Devuelve (discrepancias, ausentes)."""
    malos, ausentes = {}, []
    for oid, skus in esperado.items():
        o = post.get(oid)
        if o is None:
            ausentes.append(oid)
            continue
        for sku, locs in skus.items():
            e = {_canon(k): v for k, v in locs.items()}
            if o["ubic"].get(sku, {}) != e:
                malos.setdefault(oid, {})[sku] = {"esperado": e, "real": o["ubic"].get(sku, {})}
    return malos, ausentes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _args(a, *nombres):
    out, i = {}, 0
    while i < len(a):
        k = a[i].lstrip("-")
        if k in nombres and i + 1 < len(a):
            out[k] = a[i + 1]
            i += 2
        else:
            raise NDError(f"argumento desconocido {a[i]}")
    return out


def _py(d, script, *args):
    r = subprocess.run([sys.executable, script, *args], cwd=d, capture_output=True, text=True)
    salida = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise NDError(f"{script} falló:\n{salida[-2000:]}")
    return salida


def _j(path):
    return json.load(open(path, encoding="utf-8"))


def _w(path, obj):
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


def _nodos_ubicaciones(obj):
    """Lista de nodos {id,name,isActive} desde la parte `data` de la respuesta de Q_LOCS (con o sin envoltorios)."""
    if isinstance(obj, dict):
        if isinstance(obj.get("nodes"), list) and obj["nodes"] and "name" in obj["nodes"][0]:
            return obj["nodes"]
        for v in obj.values():
            r = _nodos_ubicaciones(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _nodos_ubicaciones(v)
            if r:
                return r
    return []


def cmd_preparar(a):
    t0 = time.time()
    o = _args(a, "out", "scripts", "fecha", "bulks", "locations")
    d, sd = o["out"], o["scripts"]
    fecha = o.get("fecha") or datetime.datetime.now(MADRID).strftime("%d-%m-%Y")
    os.makedirs(os.path.join(d, "lotes"), exist_ok=True)
    metas = _j(o.get("bulks", os.path.join(d, "bulks.json")))
    locs = _nodos_ubicaciones(_j(o.get("locations", os.path.join(d, "locations.json"))))
    if not locs:
        raise NDError("No hay ubicaciones en locations.json (¿se leyó Q_LOCS por Composio?).")
    res = {"generado_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "fecha": fecha,
           "modo": "composio", "script_sha256": hash_canonico()}
    for f in ("engine.py", "diff.py", "build_changes_preview.py"):
        shutil.copy(os.path.join(sd, f), os.path.join(d, f))
    d10, loc_ids = d10_desde_ubicaciones(locs)
    res["d10_gid"] = d10
    res["bulks"] = {}
    for nombre, destino in (("pedidos", "orders_raw.jsonl"), ("inventario", "inventory_raw.jsonl")):
        if nombre not in metas:
            raise NDError(f"Falta la bulk '{nombre}' en bulks.json.")
        v = bajar_bulk(metas[nombre], os.path.join(d, destino), nombre)
        res["bulks"][nombre] = {"id": metas[nombre].get("id"), **v}
    falt, universo = faltantes_d10(os.path.join(d, "inventory_raw.jsonl"))
    res["d10"] = {"universo": universo, "faltantes": len(falt), "tope": TOPE_D10}
    log = _py(d, "engine.py") + _py(d, "diff.py") + _py(d, "build_changes_preview.py", fecha)
    open(os.path.join(d, "pipeline.log"), "w", encoding="utf-8").write(log)
    results = pickle.load(open(os.path.join(d, "scope.pkl"), "rb"))
    diff = pickle.load(open(os.path.join(d, "diff.pkl"), "rb"))
    tags = {}
    for t in diff["tagsRemove"]:
        tags.setdefault(t["id"], {"remove": [], "add": []})["remove"] += t["tags"]
    for t in diff["tagsAdd"]:
        tags.setdefault(t["id"], {"remove": [], "add": []})["add"] += t["tags"]
    fos = plan_fos(results, diff)
    res["plan"] = {"pedidos_con_tags": len(tags), "tagsRemove": len(diff["tagsRemove"]), "tagsAdd": len(diff["tagsAdd"]),
                   "moves_diff": len(diff["moves"]), "splits_diff": len(diff["splits"]),
                   "pedidos_con_fos": len(fos), "ops_fos": sum(len(p["ops"]) for p in fos.values())}
    motivo = ("" if not falt else "tope de 25 superado: NO se activa" if len(falt) > TOPE_D10 else
              "D10 no resuelto por nombre: NO se activa" if not d10 else "pendiente (lotes act_*)")
    for v in falt:
        v["activado"] = motivo
    res["d10"]["motivo"] = motivo
    _w(os.path.join(d, "d10_pending.json"), falt)
    _w(os.path.join(d, "plan_almacenes.json"), {"tags": tags, "fos": fos})
    _w(os.path.join(d, "loc_ids.json"), loc_ids)
    foto = tags_de_jsonl(os.path.join(d, "orders_raw.jsonl"))
    faltan = sorted(set(tags) - set(foto))
    if faltan:
        raise NDError(f"Pedidos del plan que no están en la bulk de pedidos: {faltan[:10]}")
    _w(os.path.join(d, "antes.json"), {oid: foto[oid] for oid in tags})
    indice = []
    lts = lotes_hibridos(falt, d10, tags)
    for l in lts:
        _w(os.path.join(d, "lotes", l["nombre"] + ".json"), l)
        open(os.path.join(d, "lotes", l["nombre"] + ".graphql"), "w", encoding="utf-8").write(l["query"])
        indice.append({"lote": l["nombre"], "campo": l["campo"], "n": len(l["ops"])})
    if any(l["bloque"] == "act" for l in lts):
        open(os.path.join(d, "lectura_act.graphql"), "w", encoding="utf-8").write(
            doc_lectura_act([v["inventory_item_id"] for v in falt]))
    st = {"cola": cola_fos(fos), "i": 0, "sub": None, "pendiente": None, "ok": [], "rechazos": [], "abortado": None}
    _w(os.path.join(d, "fos_estado.json"), st)
    if st["cola"]:
        open(os.path.join(d, "fo_lectura.graphql"), "w", encoding="utf-8").write(doc_lectura_fos([st["cola"][0]["oid"]]))
    res["lotes"] = indice
    res["pasos_fos_previstos"] = len(st["cola"])
    res["segundos"] = round(time.time() - t0, 1)
    _w(os.path.join(d, "preparar.json"), res)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


def _tags_reales(d, bulk_json, nombre):
    meta = _j(bulk_json)
    pth = os.path.join(d, nombre)
    bajar_bulk(meta, pth, "tags")
    return tags_de_jsonl(pth)


def cmd_verificar(a):
    o = _args(a, "out", "hasta", "bulk", "lectura")
    d, hasta = o["out"], o["hasta"]
    ausentes = []
    if hasta == "act":
        falt = _j(os.path.join(d, "d10_pending.json"))
        ids = [v["inventory_item_id"] for v in falt]
        lec = _j(o.get("lectura", os.path.join(d, "lectura_act.json")))
        items = []

        def walk(x):
            if isinstance(x, dict):
                if str(x.get("id", "")).startswith("gid://shopify/InventoryItem/") and "inventoryLevels" in x:
                    items.append(x)
                    return
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(lec)
        con = {i["id"] for i in items if {x["location"]["name"] for x in i["inventoryLevels"]["nodes"]} & D10_ALIASES}
        malos = {v["sku"]: "sin nivel en D10" for v in falt if v["inventory_item_id"] not in con}
        n = len(ids)
    elif hasta in ("t1", "t2"):
        plan = _j(os.path.join(d, "plan_almacenes.json"))["tags"]
        antes = _j(os.path.join(d, "antes.json"))
        esp = estado_tras_fase(antes, plan, 1 if hasta == "t1" else 2)
        real = _tags_reales(d, o.get("bulk", os.path.join(d, "tags_bulk.json")), "tags_bulk.jsonl") if esp else {}
        malos, ausentes = verificar_tags(esp, real)
        n = len(esp)
    else:
        raise NDError("--hasta debe ser act, t1 o t2")
    _w(os.path.join(d, f"verificar_{hasta}.json"), {"hasta": hasta, "n": n, "discrepancias": malos, "no_releidos": ausentes})
    print(f"VERIFICAR {hasta}: {n} esperados, {len(malos)} con discrepancias, {len(ausentes)} no releídos")
    for k, v in list(malos.items())[:40]:
        print(k, json.dumps(v, ensure_ascii=False))
    if malos:
        print("DISCREPANCIAS: NO lanzar los lotes siguientes ni los FOs.")
        raise SystemExit(3)
    if ausentes:
        print("NO RELEÍDOS (no vienen en la bulk de tags; comprobar uno a uno con SHOPIFY_QUERY_NODE antes de seguir):")
        for k in ausentes[:40]:
            print(k)
        raise SystemExit(4)
    print("OK: se puede seguir.")


def _emitir_lectura(d, oids):
    docs = docs_lectura_fos(oids)
    _escribir_lecturas(os.path.join(d, "fo_lectura.graphql"), docs)
    return docs


def cmd_siguiente(a):
    """Bucle de FOs. Cada llamada imprime UNA de estas cosas:
      PASO n/N ...  -> ejecutar con Composio out/fo_paso.graphql; después ejecutar out/fo_lectura.graphql
                       (relectura de los pedidos que indica), escribir su `data` en out/fo_lectura.json y volver
                       a llamar:  siguiente --out out --lectura out/fo_lectura.json
                       (si la respuesta del paso trae userErrors:  siguiente --out out --rechazo "<texto>")
      LEER oids     -> falta la lectura de esos pedidos: ejecutar out/fo_lectura.graphql, escribir su `data` en
                       out/fo_lectura.json y volver a llamar con --lectura
      FIN           -> acabó (exit 0)
      ABORTADO      -> exit 3: no ejecutar nada más."""
    o = _args(a, "out", "rechazo", "lectura")
    d = o["out"]
    ruta = os.path.join(d, "fos_estado.json")
    st = _j(ruta)
    loc_ids = _j(os.path.join(d, "loc_ids.json"))
    paso_f = os.path.join(d, "fo_paso.graphql")
    lect = nodos_lectura(_j(o["lectura"])) if o.get("lectura") else {}

    def guardar():
        _w(ruta, st)

    def pedir(oids):
        docs = _emitir_lectura(d, oids)
        guardar()
        print(f"LEER {', '.join(oids)}\n" + INSTR_LECTURA.format(n=len(docs), f=os.path.join(d, 'fo_lectura.json'))
              + f" y correr: siguiente --out {d} --lectura {os.path.join(d, 'fo_lectura.json')}\n{_texto_lecturas(docs)}")

    if st.get("abortado"):
        print(f"ABORTADO: {st['abortado']}")
        raise SystemExit(3)
    if st.get("pendiente"):
        it = st["cola"][st["i"]]
        if o.get("rechazo"):
            st["rechazos"].append({"pedido": it["pedido"], "op": it["op"], "paso": st["pendiente"]["paso"], "error": o["rechazo"]})
            st["i"] += 1
            st["sub"] = None
            st.pop("fo_sep", None)
        else:
            if it["oid"] not in lect:
                return pedir([it["oid"]])
            motivo = confirmar_fo(st, fos_de_nodo(lect[it["oid"]]))
            if motivo:
                st["abortado"] = f"{it['pedido']}: {motivo}"
                st["pendiente"] = None
                guardar()
                print(f"ABORTADO: {st['abortado']} — NO seguir con más FOs.")
                raise SystemExit(3)
        st["pendiente"] = None
    while st["i"] < len(st["cola"]):
        it = st["cola"][st["i"]]
        if it["oid"] not in lect:
            return pedir([it["oid"]])
        p, rech = paso_fo(st, fos_de_nodo(lect[it["oid"]]), loc_ids)
        if rech:
            st["rechazos"].append({"pedido": it["pedido"], "op": it["op"], "error": rech})
            st["i"] += 1
            st["sub"] = None
            continue
        st["pendiente"] = {k: v for k, v in p.items() if k != "query"}
        open(paso_f, "w", encoding="utf-8").write(p["query"])
        oids = [it["oid"]]
        if p["paso"] != "split" and st["i"] + 1 < len(st["cola"]) and st["cola"][st["i"] + 1]["oid"] != it["oid"]:
            oids.append(st["cola"][st["i"] + 1]["oid"])
        docs = _emitir_lectura(d, oids)
        guardar()
        print(f"PASO {st['i'] + 1}/{len(st['cola'])} {it['pedido']} {p['paso']} {it['op']['from']} -> {it['op']['to']} "
              f"skus={p['skus']}\nEjecutar con SHOPIFY_GRAPH_QL_ADMIN_EXECUTE el contenido EXACTO de {paso_f}; "
              f"después, en OTRO bloque, la relectura ({len(oids)} pedido(s)). "
              + INSTR_LECTURA.format(n=len(docs), f=os.path.join(d, 'fo_lectura.json')) + f".\n{_texto_lecturas(docs)}")
        return
    guardar()
    for f in (paso_f, os.path.join(d, "fo_lectura.graphql")):
        if os.path.exists(f):
            os.remove(f)
    print(f"FIN: {len(st['ok'])} ok, {len(st['rechazos'])} rechazos.")


def _args_lote(a):
    """Como _args, pero --rechazo puede repetirse (alias=texto)."""
    out, rech, i = {}, {}, 0
    while i < len(a):
        k = a[i].lstrip("-")
        if k in ("out", "lectura") and i + 1 < len(a):
            out[k] = a[i + 1]
            i += 2
        elif k == "rechazo" and i + 1 < len(a):
            alias, _, txt = a[i + 1].partition("=")
            rech[alias.strip()] = txt.strip() or "userErrors sin texto"
            i += 2
        else:
            raise NDError(f"argumento desconocido {a[i]}")
    return out, rech


def cmd_lote(a):
    """Bucle de FOs POR LOTES (v77): hasta LOTE_FO pasos de PEDIDOS DISTINTOS en UNA sola mutación con alias
    (m0, m1, ...). Misma lógica de pasos/confirmación que `siguiente` (paso_fo / confirmar_fo), pero la
    relectura de cada ronda verifica el lote anterior Y trae ya los pedidos de la ronda siguiente.
    Cada llamada imprime UNA de estas cosas:
      LEER ...  -> ejecutar las relecturas que imprime (v83: uno o varios documentos de <= 6 pedidos, todos en UN
                   bloque), escribir en out/fo_lectura.json la LISTA de sus `data` y volver a llamar con --lectura.
      LOTE ...  -> ejecutar la MUTACIÓN impresa (un bloque) y DESPUÉS, en otro bloque, la RELECTURA impresa;
                   escribir su `data` en out/fo_lectura.json y llamar:  lote --out out --lectura out/fo_lectura.json
                   [--rechazo m3="texto de los userErrors" ...]   (un --rechazo por cada alias con userErrors)
      FIN       -> acabó (exit 0).      ABORTADO -> exit 3: no ejecutar nada más."""
    o, rech = _args_lote(a)
    d = o["out"]
    ruta = os.path.join(d, "fos_estado.json")
    st = _j(ruta)
    loc_ids = _j(os.path.join(d, "loc_ids.json"))
    cola = st["cola"]
    lect = nodos_lectura(_j(o["lectura"])) if o.get("lectura") else {}
    paso_f = os.path.join(d, "fo_paso.graphql")
    lec_f = os.path.join(d, "fo_lectura.graphql")

    def guardar():
        _w(ruta, st)

    def pedir(oids):
        docs = docs_lectura_fos(oids)
        _escribir_lecturas(lec_f, docs)
        guardar()
        print(f"LEER {len(oids)} pedido(s) en {len(docs)} documento(s)\n"
              + INSTR_LECTURA.format(n=len(docs), f=os.path.join(d, 'fo_lectura.json'))
              + f", y correr: lote --out {d} --lectura {os.path.join(d, 'fo_lectura.json')}\n{_texto_lecturas(docs)}")

    if st.get("abortado"):
        print(f"ABORTADO: {st['abortado']}")
        raise SystemExit(3)

    # 1) confirmar el lote pendiente
    pend = st.get("lote")
    if pend:
        faltan = [cola[p["k"]]["oid"] for p in pend if cola[p["k"]]["oid"] not in lect]
        if faltan:
            return pedir(sorted(set(faltan)))
        abortos = []
        for p in pend:
            k = p["k"]
            it = cola[k]
            if p["alias"] in rech:
                st["rechazos"].append({"pedido": it["pedido"], "op": it["op"], "paso": p["paso"], "error": rech[p["alias"]]})
                it["fin"] = True
                st["i"] += 1
                it.pop("fo_sep", None)
                it["sub"] = None
                continue
            sh = {"cola": cola, "i": k, "sub": it.get("sub"), "pendiente": {x: p[x] for x in ("paso", "skus", "from", "to") if x in p},
                  "ok": st["ok"]}
            motivo = confirmar_fo(sh, fos_de_nodo(lect[it["oid"]]))
            if motivo:
                abortos.append(f"{it['pedido']}: {motivo}")
                continue
            it["sub"] = sh.get("sub")
            if "fo_sep" in sh:
                it["fo_sep"] = sh["fo_sep"]
            else:
                it.pop("fo_sep", None)
            if sh["i"] > k:
                it["fin"] = True
                st["i"] += 1
        st["lote"] = None
        if abortos:
            st["abortado"] = " | ".join(abortos)
            guardar()
            print(f"ABORTADO: {st['abortado']} — NO seguir con más FOs.")
            raise SystemExit(3)

    # 2) siguiente lote
    cabezas = defaultdict(list)
    for k, it in enumerate(cola):
        if not it.get("fin"):
            cabezas[it["oid"]].append(k)
    lote, usados = [], set()
    for oid, ks in cabezas.items():
        if len(lote) >= LOTE_FO or oid not in lect:
            continue
        for k in ks:
            it = cola[k]
            sh = {"cola": cola, "i": k, "sub": it.get("sub"), "fo_sep": it.get("fo_sep")}
            p, rec = paso_fo(sh, fos_de_nodo(lect[oid]), loc_ids)
            if rec:
                st["rechazos"].append({"pedido": it["pedido"], "op": it["op"], "error": rec})
                it["fin"] = True
                it["sub"] = None
                it.pop("fo_sep", None)
                st["i"] += 1
                continue
            lote.append((k, p))
            usados.add(oid)
            break
    pendientes = [oid for oid in cabezas if any(not cola[k].get("fin") for k in cabezas[oid])]
    if not lote:
        sin_leer = [oid for oid in pendientes if oid not in lect][:LOTE_FO]
        if sin_leer:
            return pedir(sin_leer)
        guardar()
        for f in (paso_f, lec_f):
            if os.path.exists(f):
                os.remove(f)
        print(f"FIN: {len(st['ok'])} ok, {len(st['rechazos'])} rechazos.")
        return
    campos, st["lote"] = [], []
    for j, (k, p) in enumerate(lote):
        campos.append(f"m{j}: " + p["query"][len("mutation { "):-len(" }")])
        st["lote"].append({"k": k, "alias": f"m{j}", **{x: v for x, v in p.items() if x != "query"}})
    doc_mut = "mutation { " + " ".join(campos) + " }"
    oids_batch = [cola[k]["oid"] for k, _ in lote]
    prefetch = [oid for oid in pendientes if oid not in usados][:LOTE_FO]
    docs_lec = docs_lectura_fos(oids_batch + prefetch)
    open(paso_f, "w", encoding="utf-8").write(doc_mut)
    _escribir_lecturas(lec_f, docs_lec)
    guardar()
    res = defaultdict(int)
    for _, p in lote:
        res[p["paso"]] += 1
    print(f"LOTE {len(lote)} paso(s) ({', '.join(f'{v} {k}' for k, v in res.items())}) · hechos {st['i']}/{len(cola)}\n"
          f"1) Ejecutar SOLO esta MUTACIÓN con SHOPIFY_GRAPH_QL_ADMIN_EXECUTE (un bloque, texto EXACTO). Todos los userErrors "
          f"deben estar vacíos; si algún alias trae userErrors, anotar alias y texto.\n=== MUTACION ===\n{doc_mut}\n=== FIN ===\n"
          f"2) DESPUÉS, en OTRO bloque, la RELECTURA ({len(oids_batch)} a verificar + {len(prefetch)} del lote siguiente) en "
          f"{len(docs_lec)} documento(s). " + INSTR_LECTURA.format(n=len(docs_lec), f=os.path.join(d, 'fo_lectura.json'))
          + f".\n{_texto_lecturas(docs_lec)}\n"
          f"3) Correr: lote --out {d} --lectura {os.path.join(d, 'fo_lectura.json')}"
          f"  (+ un  --rechazo mN=\"texto\"  por cada alias con userErrors)")


def cmd_cerrar(a):
    o = _args(a, "out", "bulk")
    d = o["out"]
    plan = _j(os.path.join(d, "plan_almacenes.json"))
    st = _j(os.path.join(d, "fos_estado.json"))
    antes = _j(os.path.join(d, "antes.json"))
    meta = _j(o.get("bulk", os.path.join(d, "final_bulk.json")))
    pth = os.path.join(d, "final_raw.jsonl")
    bajar_bulk(meta, pth, "pedidos")
    post = estado_jsonl(pth)
    esp_tags = estado_tras_fase(antes, plan["tags"], 2)
    reales = {oid: post[oid]["tags"] for oid in esp_tags if oid in post}
    tags_mal, tags_aus = verificar_tags(esp_tags, reales)
    esperado = {oid: p["esperado"] for oid, p in plan["fos"].items()}
    ubic_mal, ubic_aus = comparar_ubicaciones(esperado, post)
    res = {"tags_pedidos": len(esp_tags), "tags_discrepancias": tags_mal, "tags_no_releidos": tags_aus,
           "fos_ok": st["ok"], "fos_rechazos": st["rechazos"], "fos_abortado": st.get("abortado"),
           "fos_pasos_hechos": st["i"], "fos_pasos_previstos": len(st["cola"]),
           "ubicaciones_discrepancias": ubic_mal, "ubicaciones_no_releidas": ubic_aus}
    _w(os.path.join(d, "cerrar.json"), res)
    print(json.dumps({"tags_pedidos": res["tags_pedidos"], "tags_discrepancias": len(tags_mal),
                      "tags_no_releidos": len(tags_aus), "fos_ok": len(st["ok"]), "fos_rechazos": len(st["rechazos"]),
                      "fos_abortado": st.get("abortado"), "ubicaciones_discrepancias": len(ubic_mal),
                      "ubicaciones_no_releidas": len(ubic_aus)}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# Selftest + hash
# ---------------------------------------------------------------------------

def hash_canonico():
    with open(os.path.abspath(__file__), "rb") as f:
        t = f.read().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return hashlib.sha256((t + "\n").encode("utf-8")).hexdigest()


def _selftest():
    import contextlib
    import io
    import tempfile
    fallos = []

    def ok(c, m):
        if not c:
            fallos.append(m)

    # --- descarga validada ---
    with tempfile.TemporaryDirectory() as tmp:
        pth = os.path.join(tmp, "x.jsonl")
        with open(pth, "w", encoding="utf-8") as f:
            f.write('{"id":"A","tags":["x"]}\n{"id":"B","tags":[]}\n{"id":"C","__parentId":"A"}\n')
        ok(validar_descarga(pth, "3", str(os.path.getsize(pth)))["lineas"] == 3, "descarga válida rechazada")
        for oc, fs in (("4", str(os.path.getsize(pth))), ("3", "5")):
            try:
                validar_descarga(pth, oc, fs)
                ok(False, f"descarga inválida aceptada ({oc},{fs})")
            except NDError:
                pass
        ok(tags_de_jsonl(pth) == {"A": ["x"], "B": []}, "tags_de_jsonl")

        # --- §2B ---
        p = os.path.join(tmp, "i.jsonl")
        L = [{"id": "gid://shopify/ProductVariant/1", "sku": "ND-1", "product": {"status": "ACTIVE"}, "inventoryItem": {"id": "I1"}},
             {"location": {"name": "Terrassa"}, "__parentId": "gid://shopify/ProductVariant/1"},
             {"id": "gid://shopify/ProductVariant/2", "sku": "ND-2", "product": {"status": "ACTIVE"}, "inventoryItem": {"id": "I2"}},
             {"location": {"name": "DHL"}, "__parentId": "gid://shopify/ProductVariant/2"},
             {"id": "gid://shopify/ProductVariant/3", "sku": "PZ-3", "product": {"status": "ACTIVE"}, "inventoryItem": {"id": "I3"}},
             {"id": "gid://shopify/ProductVariant/4", "sku": "ND-4", "product": {"status": "ARCHIVED"}, "inventoryItem": {"id": "I4"}},
             {"id": "gid://shopify/ProductVariant/5", "sku": "", "product": {"status": "ACTIVE"}, "inventoryItem": {"id": "I5"}}]
        open(p, "w").write("\n".join(json.dumps(x) for x in L))
        f_, u = faltantes_d10(p)
        ok(u == 2 and [x["sku"] for x in f_] == ["ND-1"] and f_[0]["falta_matadepera"], f"§2B {f_} {u}")

    # --- ubicaciones / D10 por nombre ---
    locs = [{"id": "LD", "name": "D10", "isActive": True}, {"id": "LM", "name": "Matadepera", "isActive": True}]
    ok(d10_desde_ubicaciones(locs) == ("LD", {"D10": "LD", "Matadepera": "LM"}), "d10 por nombre")
    ok(d10_desde_ubicaciones(locs + [{"id": "LX", "name": "DHL", "isActive": False}])[0] is None, "dos D10/DHL no se activa")
    ok(d10_desde_ubicaciones([locs[1]])[0] is None, "sin D10 no se activa")
    ok(_nodos_ubicaciones({"data": {"data": {"locations": {"nodes": locs}}}}) == locs, "envoltorios de locations")

    # --- tags ---
    ok(fases_tags({"A": {"remove": ["x"], "add": ["y"]}}) == ([{"id": "A", "tags": ["x"]}], [{"id": "A", "tags": ["y"]}]), "fases")
    try:
        fases_tags({"A": {"remove": ["x"], "add": ["X"]}})
        ok(False, "plan contradictorio aceptado")
    except NDError:
        pass
    ok(estado_tras_fase({"A": ["x", "z"]}, {"A": {"remove": ["x"], "add": ["y"]}}, 1)["A"] == ["z"], "estado fase 1")
    ok(estado_tras_fase({"A": ["x", "z"]}, {"A": {"remove": ["x"], "add": ["y"]}}, 2)["A"] == ["z", "y"], "estado fase 2")
    m, aus = verificar_tags({"O1": ["a", "B"]}, {"O1": ["b", "A"]})
    ok(m == {} and aus == [], "verificación sin distinguir mayúsculas")
    m, aus = verificar_tags({"O1": ["a", "b"]}, {"O1": ["a"]})
    ok(m.get("O1", {}).get("faltan") == ["b"], "tag perdido no detectado")
    m, aus = verificar_tags({"O1": ["a"]}, {})
    ok(m == {} and aus == ["O1"], "pedido no releído debe ir a ausentes")

    # --- plan de FOs ---
    def O(name, fos, tags=()):
        return {"order": {"id": "O" + name, "name": name, "tags": list(tags), "fos": fos}}
    results = [
        O("#m", [{"id": "F1", "status": "OPEN", "location": "Terrassa", "lines": [{"id": "L1", "sku": "A", "remainingQuantity": 1}]}]),
        O("#s", [{"id": "F2", "status": "OPEN", "location": "D10", "lines": [{"id": "L2", "sku": "B", "remainingQuantity": 2},
                                                                           {"id": "L3", "sku": "C", "remainingQuantity": 1}]}]),
    ]
    diff = {"moves": [{"order": "#m", "fo_id": "F1", "from": "Terrassa", "to": "D10"}],
            "splits": [{"order": "#s", "decisions": [{"line_id": "L2", "destino": "D10"}, {"line_id": "L3", "destino": "Matadepera"}]}],
            "tagsAdd": [], "tagsRemove": []}
    pl = plan_fos(results, diff)
    ok(pl["O#m"]["ops"] == [{"tipo": "move", "fo_id": "F1", "from": "Terrassa", "to": "D10"}]
       and pl["O#m"]["esperado"] == {"A": {"D10": 1}}, f"move {pl['O#m']}")
    ops = pl["O#s"]["ops"]
    ok(len(ops) == 1 and ops[0]["tipo"] == "split_move" and ops[0]["to"] == "Matadepera"
       and [l["sku"] for l in ops[0]["lineas"]] == ["C"], f"split {ops}")
    ok(pl["O#s"]["esperado"] == {"B": {"D10": 2}, "C": {"Matadepera": 1}}, "esperado split")

    # --- lotes ---
    falt2 = [{"inventory_item_id": f"I{k}", "sku": f"S{k}"} for k in range(20)]
    tg = {"OA": {"remove": ["TERRASA"], "add": ["D10"]}, "OB": {"remove": [], "add": ["D10"]}}
    lh = lotes_hibridos(falt2, "LOCD10", tg)
    ok([l["nombre"] for l in lh] == ["act_01", "act_02", "t1_01", "t2_01"], f"lotes {[l['nombre'] for l in lh]}")
    ok(all(sum(c in l["query"] for c in ("tagsAdd(", "tagsRemove(", "inventoryActivate(")) == 1 for l in lh), "lote mezcla tipos")
    ok(len(lh[0]["ops"]) == LOTE_D10 and "available" not in lh[0]["query"] and "onHand" not in lh[0]["query"], "act sin cantidades")
    ok(lotes_hibridos(falt2 + [{"inventory_item_id": "Ix", "sku": "x"}] * 6, "LOCD10", {}) == [], "tope 25 respetado")
    ok(lotes_hibridos(falt2[:3], None, {}) == [], "sin D10 no se activa")
    ok(doc_lectura_fos(["O1", "O2"]).startswith('query { n0: node(id: "O1") { ... on Order {') and "n1: node" in doc_lectura_fos(["O1", "O2"]),
       "doc lectura fos")
    ok(doc_lectura_act(["I1"]) == 'query { nodes(ids: ["I1"]) { ' + ACT_FRAG + ' } }', "doc lectura act")

    # --- lectura: envoltorios ---
    def nodo(oid, fos):
        return {"id": oid, "name": oid, "fulfillmentOrders": {"nodes": [
            {"id": f, "status": "OPEN", "assignedLocation": {"name": loc},
             "lineItems": {"nodes": [{"id": lid, "remainingQuantity": q, "lineItem": {"sku": sku}} for lid, sku, q in ls]}}
            for f, loc, ls in fos]}}
    n1 = nodo("gid://shopify/Order/1", [("F1", "Terrassa", [("L1", "A", 1)])])
    ok(set(nodos_lectura({"data": {"data": {"n0": n1}}})) == {"gid://shopify/Order/1"}, "envoltorios de lectura")
    ok(fos_de_nodo(n1) == {"F1": {"loc": "Terrassa", "lines": {"L1": ("A", 1)}}}, "fos_de_nodo")

    # --- bucle completo (protocolo LEER / PASO) en un directorio temporal ---
    with tempfile.TemporaryDirectory() as tmp:
        OM, OS = "gid://shopify/Order/1", "gid://shopify/Order/2"
        cola = [{"oid": OM, "pedido": "#m", "op": {"tipo": "move", "fo_id": "F1", "from": "Terrassa", "to": "D10"}},
                {"oid": OS, "pedido": "#s", "op": {"tipo": "split_move", "fo_id": "F2", "from": "D10", "to": "Matadepera",
                                                   "lineas": [{"id": "L3", "quantity": 1, "sku": "C"}]}}]
        _w(os.path.join(tmp, "fos_estado.json"), {"cola": cola, "i": 0, "sub": None, "pendiente": None, "ok": [], "rechazos": [], "abortado": None})
        _w(os.path.join(tmp, "loc_ids.json"), {"D10": "LD", "Matadepera": "LM", "Terrassa": "LT"})

        def sig(*args, lectura=None):
            if lectura is not None:
                _w(os.path.join(tmp, "l.json"), lectura)
                args = args + ("--lectura", os.path.join(tmp, "l.json"))
            buf = io.StringIO()
            code = 0
            with contextlib.redirect_stdout(buf):
                try:
                    cmd_siguiente(["--out", tmp, *args])
                except SystemExit as e:
                    code = e.code
            return buf.getvalue(), code

        s_m0 = nodo(OM, [("F1", "Terrassa", [("L1", "A", 1)])])
        s_s0 = nodo(OS, [("F2", "D10", [("L2", "B", 2), ("L3", "C", 1)])])
        out, c = sig()
        ok(out.startswith("LEER " + OM) and c == 0, f"pide lectura inicial: {out[:60]}")
        out, c = sig(lectura={"n0": s_m0})
        ok(out.startswith("PASO 1/2") and "fulfillmentOrderMove" in open(os.path.join(tmp, "fo_paso.graphql")).read(), f"paso 1: {out[:60]}")
        lec = open(os.path.join(tmp, "fo_lectura.graphql")).read()
        ok(OM in lec and OS in lec, "relectura posterior incluye el pedido siguiente")
        out, c = sig()  # sin lectura tras el paso: debe pedirla
        ok(out.startswith("LEER " + OM), "sin lectura tras el paso debe pedirla")
        s_m_no = nodo(OM, [("F1", "Terrassa", [("L1", "A", 1)])])
        out, c = sig(lectura={"n0": s_m_no, "n1": s_s0})  # el move NO se aplicó
        ok(c == 3 and out.startswith("ABORTADO"), f"move no aplicado debe abortar: {out[:60]}")
        out, c = sig()
        ok(c == 3, "tras abortar sigue abortado")
        # camino feliz
        st = _j(os.path.join(tmp, "fos_estado.json"))
        st.update({"abortado": None, "pendiente": None, "i": 0, "sub": None, "ok": [], "rechazos": []})
        _w(os.path.join(tmp, "fos_estado.json"), st)
        sig(lectura={"n0": s_m0})
        s_m1 = nodo(OM, [("F9", "DHL", [("L1", "A", 1)])])
        out, c = sig(lectura={"n0": s_m1, "n1": s_s0})
        ok(out.startswith("PASO 2/2") and "fulfillmentOrderSplit" in open(os.path.join(tmp, "fo_paso.graphql")).read()
           and '{id: "L3", quantity: 1}' in open(os.path.join(tmp, "fo_paso.graphql")).read(), f"paso 2 split: {out[:80]}")
        s_s1 = nodo(OS, [("F2", "D10", [("L2", "B", 2)]), ("F7", "D10", [("L8", "C", 1)])])
        out, c = sig(lectura={"n0": s_s1})
        ok(out.startswith("PASO 2/2") and "fulfillmentOrderMove" in open(os.path.join(tmp, "fo_paso.graphql")).read()
           and '"F7"' in open(os.path.join(tmp, "fo_paso.graphql")).read() and '"LM"' in open(os.path.join(tmp, "fo_paso.graphql")).read(),
           f"move tras split: {out[:80]}")
        s_s2 = nodo(OS, [("F2", "D10", [("L2", "B", 2)]), ("F7", "Matadepera", [("L8", "C", 1)])])
        out, c = sig(lectura={"n0": s_s2})
        ok(out.startswith("FIN: 2 ok, 0 rechazos") and c == 0, f"fin: {out[:60]}")
        ok(not os.path.exists(os.path.join(tmp, "fo_paso.graphql")), "fo_paso.graphql debe borrarse al terminar")
        # rechazo
        st = _j(os.path.join(tmp, "fos_estado.json"))
        st.update({"cola": cola[:1], "i": 0, "sub": None, "pendiente": None, "ok": [], "rechazos": [], "abortado": None})
        _w(os.path.join(tmp, "fos_estado.json"), st)
        sig(lectura={"n0": s_m0})
        out, c = sig("--rechazo", "sin existencias", )
        ok(out.startswith("FIN: 0 ok, 1 rechazos") and c == 0, f"rechazo: {out[:60]}")

    # --- bucle por LOTES (v77) ---
    global LOTE_FO
    lote_prev, LOTE_FO = LOTE_FO, 2
    try:
        with tempfile.TemporaryDirectory() as tmp:
            OA, OB, OC = (f"gid://shopify/Order/{n}" for n in (11, 12, 13))
            cola = [{"oid": OA, "pedido": "#a", "op": {"tipo": "move", "fo_id": "FA", "from": "Terrassa", "to": "D10"}},
                    {"oid": OB, "pedido": "#b", "op": {"tipo": "split_move", "fo_id": "FB", "from": "D10", "to": "Matadepera",
                                                       "lineas": [{"id": "LB2", "quantity": 1, "sku": "C"}]}},
                    {"oid": OC, "pedido": "#c", "op": {"tipo": "move", "fo_id": "FC", "from": "Terrassa", "to": "D10"}}]
            _w(os.path.join(tmp, "fos_estado.json"), {"cola": cola, "i": 0, "sub": None, "pendiente": None, "ok": [], "rechazos": [], "abortado": None})
            _w(os.path.join(tmp, "loc_ids.json"), {"D10": "LD", "Matadepera": "LM", "Terrassa": "LT"})

            def lot(*args, lectura=None):
                if lectura is not None:
                    _w(os.path.join(tmp, "l.json"), lectura)
                    args = args + ("--lectura", os.path.join(tmp, "l.json"))
                buf, code = io.StringIO(), 0
                with contextlib.redirect_stdout(buf):
                    try:
                        cmd_lote(["--out", tmp, *args])
                    except SystemExit as e:
                        code = e.code
                return buf.getvalue(), code
            nA = nodo(OA, [("FA", "Terrassa", [("LA", "A", 1)])])
            nB = nodo(OB, [("FB", "D10", [("LB1", "B", 2), ("LB2", "C", 1)])])
            nC = nodo(OC, [("FC", "Terrassa", [("LC", "E", 1)])])
            out, c = lot()
            ok(out.startswith("LEER 2 pedido(s)") and OA in out and OB in out and OC not in out, f"lote: lectura inicial {out[:40]}")
            out, c = lot(lectura={"n0": nA, "n1": nB})
            mut = open(os.path.join(tmp, "fo_paso.graphql")).read()
            ok(out.startswith("LOTE 2 paso(s)") and mut.count("m0: fulfillmentOrderMove") == 1 and "m1: fulfillmentOrderSplit" in mut
               and mut.startswith("mutation { m0:") and OC in open(os.path.join(tmp, "fo_lectura.graphql")).read(), f"lote 1: {out[:60]}")
            nA2 = nodo(OA, [("FX", "D10", [("LA", "A", 1)])])
            nB2 = nodo(OB, [("FB", "D10", [("LB1", "B", 2)]), ("FS", "D10", [("LB2", "C", 1)])])
            out, c = lot(lectura={"n0": nA2, "n1": nB2, "n2": nC})
            mut = open(os.path.join(tmp, "fo_paso.graphql")).read()
            ok(out.startswith("LOTE 2 paso(s)") and '"FS"' in mut and '"LM"' in mut and '"FC"' in mut, f"lote 2 (move tras split + prefetch): {out[:60]}")
            nB3 = nodo(OB, [("FB", "D10", [("LB1", "B", 2)]), ("FS", "Matadepera", [("LB2", "C", 1)])])
            nC2 = nodo(OC, [("FC", "Terrassa", [("LC", "E", 1)])])
            out, c = lot("--rechazo", 'm1=sin existencias', lectura={"n0": nB3, "n1": nC2})
            ok(out.startswith("FIN: 2 ok, 1 rechazos") and c == 0, f"lote: rechazo por alias y fin: {out[:60]}")
            # abort si el lote no se aplicó
            st = _j(os.path.join(tmp, "fos_estado.json"))
            st.update({"abortado": None, "lote": None, "i": 0, "ok": [], "rechazos": []})
            for it in st["cola"]:
                it.pop("fin", None); it["sub"] = None; it.pop("fo_sep", None)
            _w(os.path.join(tmp, "fos_estado.json"), st)
            lot(); lot(lectura={"n0": nA, "n1": nB})
            out, c = lot(lectura={"n0": nA, "n1": nB, "n2": nC})
            ok(c == 3 and out.startswith("ABORTADO"), f"lote: no aplicado debe abortar: {out[:60]}")
    finally:
        LOTE_FO = lote_prev

    # --- v83: relectura partida en documentos de <= MAX_PEDIDOS_LECTURA pedidos ---
    ids15 = [f"gid://shopify/Order/{900 + k}" for k in range(15)]
    dl = docs_lectura_fos(ids15)
    ok(len(dl) == 3 and all(d_.count("node(id:") <= MAX_PEDIDOS_LECTURA for d_ in dl)
       and sum(d_.count("node(id:") for d_ in dl) == 15 and all(x in "".join(dl) for x in ids15), f"docs_lectura_fos {len(dl)}")
    ok(MAX_PEDIDOS_LECTURA * 157 < 1000, "MAX_PEDIDOS_LECTURA supera el coste máximo de Shopify")
    na, nb = nodo("gid://shopify/Order/901", []), nodo("gid://shopify/Order/902", [])
    ok(set(nodos_lectura([{"n0": na}, {"data": {"n0": nb}}])) == {"gid://shopify/Order/901", "gid://shopify/Order/902"},
       "lectura como lista de varias respuestas")
    with tempfile.TemporaryDirectory() as tmp:
        cola = [{"oid": f"gid://shopify/Order/{500 + k}", "pedido": f"#{k}",
                 "op": {"tipo": "move", "fo_id": f"F{k}", "from": "D10", "to": "Matadepera"}} for k in range(40)]
        _w(os.path.join(tmp, "fos_estado.json"), {"cola": cola, "i": 0, "sub": None, "pendiente": None, "ok": [], "rechazos": [], "abortado": None})
        _w(os.path.join(tmp, "loc_ids.json"), {"D10": "LD", "Matadepera": "LM"})

        def lot2(lectura=None):
            args = ["--out", tmp]
            if lectura is not None:
                _w(os.path.join(tmp, "l.json"), lectura)
                args += ["--lectura", os.path.join(tmp, "l.json")]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_lote(args)
            return buf.getvalue()

        def lineas_lec():
            return [x for x in open(os.path.join(tmp, "fo_lectura.graphql"), encoding="utf-8").read().splitlines() if x]
        out = lot2()
        ls_ = lineas_lec()
        ok(out.startswith("LEER 15 pedido(s) en 3 documento(s)") and len(ls_) == 3
           and all(x.count("node(id:") <= MAX_PEDIDOS_LECTURA for x in ls_) and "=== RELECTURA 3/3 ===" in out, f"v83 LEER partido: {out[:50]}")
        lec = [{"n0": nodo(c["oid"], [(c["op"]["fo_id"], "D10", [("L" + c["pedido"], "S" + c["pedido"], 1)])])} for c in cola[:15]]
        out = lot2(lec)
        ls_ = lineas_lec()
        ok(out.startswith(f"LOTE {LOTE_FO} paso(s)") and len(ls_) == 5 and all(x.count("node(id:") <= MAX_PEDIDOS_LECTURA for x in ls_)
           and sum(x.count("node(id:") for x in ls_) == 30, f"v83 LOTE con relectura partida: {out[:50]} {len(ls_)}")

    # --- estado final + comparación ---
    with tempfile.TemporaryDirectory() as tmp:
        pth = os.path.join(tmp, "f.jsonl")
        L = [{"id": "gid://shopify/Order/1", "name": "#m", "tags": ["D10"]},
             {"id": "gid://shopify/FulfillmentOrder/9", "status": "OPEN", "assignedLocation": {"name": "DHL"}, "__parentId": "gid://shopify/Order/1"},
             {"id": "gid://shopify/FulfillmentOrderLineItem/5", "remainingQuantity": 1, "lineItem": {"sku": "A"}, "__parentId": "gid://shopify/FulfillmentOrder/9"},
             {"id": "gid://shopify/Order/2", "name": "#s", "tags": []},
             {"id": "gid://shopify/FulfillmentOrder/8", "status": "CLOSED", "assignedLocation": {"name": "D10"}, "__parentId": "gid://shopify/Order/2"},
             {"id": "gid://shopify/FulfillmentOrderLineItem/4", "remainingQuantity": 1, "lineItem": {"sku": "B"}, "__parentId": "gid://shopify/FulfillmentOrder/8"}]
        open(pth, "w").write("\n".join(json.dumps(x) for x in L))
        post = estado_jsonl(pth)
        ok(post["gid://shopify/Order/1"]["ubic"] == {"A": {"D10": 1}} and post["gid://shopify/Order/2"]["ubic"] == {}, f"estado_jsonl {post}")
        mal, aus = comparar_ubicaciones({"gid://shopify/Order/1": {"A": {"D10": 1}}, "gid://shopify/Order/2": {"B": {"D10": 1}},
                                         "gid://shopify/Order/3": {"C": {"D10": 1}}}, post)
        ok(list(mal) == ["gid://shopify/Order/2"] and aus == ["gid://shopify/Order/3"], f"comparar {mal} {aus}")
    ok('orders(query: "status:open")' in Q_PEDIDOS and "displayFulfillmentStatus" in Q_PEDIDOS, "query pedidos")
    ok("shippingLine { title } discountCodes fulfillmentOrders" in Q_PEDIDOS, "query pedidos v89 (repo)")
    ok("inventoryItem { id inventoryLevels" in Q_INVENTARIO and "product { id status }" in Q_INVENTARIO, "query inventario")

    # --- v88: bulk propia (firma de su query) ---
    import re
    import pathlib
    for b, q in (("pedidos", Q_PEDIDOS), ("inventario", Q_INVENTARIO), ("tags", Q_TAGS)):
        ok(all(re.search(r"\b%s\b" % k, q) for k in FIRMAS[b][1]), f"firma {b}: clave que la query no pide")
    O = "gid://shopify/Order/1"
    ped = [{"id": O, "name": "#s1", "createdAt": "2026-10-06T04:00:00Z", "tags": ["D10"], "closed": False,
            "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED", "customer": None, "sourceName": "web",
            "totalPriceSet": {"shopMoney": {"amount": "1.00", "currencyCode": "EUR"}},
            "shippingLine": {"title": "Repo rotura s1"}, "discountCodes": ["REPOSICION-X"]},
           {"id": "gid://shopify/Order/3", "name": "#s3", "createdAt": "2026-10-06T04:00:00Z", "tags": [], "closed": False,
            "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED", "customer": None, "sourceName": "web",
            "totalPriceSet": {"shopMoney": {"amount": "1.00", "currencyCode": "EUR"}}, "shippingLine": None, "discountCodes": []},
           {"id": "gid://shopify/FulfillmentOrder/9", "status": "OPEN", "assignedLocation": {"name": "D10"},
            "deliveryMethod": {"methodType": "SHIPPING"}, "__parentId": O},
           {"id": "gid://shopify/FulfillmentOrderLineItem/5", "remainingQuantity": 1, "lineItem": {"sku": "ND-1", "name": "x"},
            "__parentId": "gid://shopify/FulfillmentOrder/9"}]
    inv = [{"id": "gid://shopify/ProductVariant/1", "sku": "ND-1", "product": {"id": "gid://shopify/Product/1", "status": "ACTIVE"},
            "inventoryItem": {"id": "gid://shopify/InventoryItem/1"}},
           {"location": {"name": "D10"}, "quantities": [{"name": "available", "quantity": 1}], "__parentId": "gid://shopify/ProductVariant/1"}]
    tgs = [{"id": O, "tags": ["D10"]}, {"id": "gid://shopify/Order/2", "tags": []}]
    fifo = [{"id": O, "name": "#s1", "createdAt": "2026-10-06T04:00:00Z", "displayFinancialStatus": "PAID", "tags": [], "closed": False,
             "email": "c@x.es", "estadoEntrega": {"value": "PREPARABLE_HOY"}, "entregaEstimada": None, "transportista": None},
            {"id": "gid://shopify/LineItem/1", "sku": "ND-1", "unfulfilledQuantity": 1, "__parentId": O}]
    a3 = [{"id": O, "name": "#s1", "createdAt": "2026-10-06T04:00:00Z", "tags": [], "displayFinancialStatus": "PAID",
           "paymentGatewayNames": [], "disputes": [], "shippingAddress": None},
          {"id": "gid://shopify/LineItem/1", "sku": "ND-1", "name": "x", "unfulfilledQuantity": 1, "__parentId": O}]
    stock = [{"id": "gid://shopify/ProductVariant/1", "sku": "ND-1", "inventoryItem": {"id": "gid://shopify/InventoryItem/1"}},
             {"location": {"id": "gid://shopify/Location/1", "name": "D10"}, "quantities": [], "__parentId": "gid://shopify/ProductVariant/1"}]
    with tempfile.TemporaryDirectory() as tmp:
        def fich(nombre, filas):
            p = os.path.join(tmp, nombre + ".jsonl")
            open(p, "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for x in filas))
            return p

        def ajena(b, filas, m):
            p = fich("aj", filas)
            try:
                validar_descarga(p, len(filas), os.path.getsize(p), b)
                ok(False, f"firma: {m} aceptada")
            except BulkAjena as e:
                ok(str(e).startswith(f"CONTENIDO AJENO ({b}): línea "), f"firma: mensaje {e}")
        for b, filas in (("pedidos", ped), ("inventario", inv), ("tags", tgs)):
            for fl in (filas, []):
                p = fich(b, fl)
                try:
                    validar_descarga(p, len(fl), os.path.getsize(p), b)
                except NDError as e:
                    ok(False, f"firma: bulk propia {b} ({len(fl)} filas) rechazada: {e}")
        ajena("pedidos", inv, "pedidos <- inventario")
        ajena("inventario", ped, "inventario <- pedidos")
        ajena("tags", fifo, "tags <- pedidos de FIFO")
        ajena("tags", ped, "tags <- pedidos")
        ajena("pedidos", tgs, "pedidos <- tags")
        ajena("pedidos", a3, "pedidos <- pedidos de A.3")
        ajena("inventario", stock, "inventario <- stock de A.5/A.1 (sin product)")
        ajena("tags", tgs + [{"__parentId": O, "x": 1}], "tags con fila hija")
        ajena("pedidos", ped + [{"id": "gid://shopify/LineItem/1", "sku": "ND-1", "__parentId": O}], "pedidos con hija LineItem")
        ajena("inventario", inv + ["x"], "inventario con fila que no es objeto")

        # extremo a extremo: exit 5 por el comando real (URL file://), y 0 con la bulk propia
        def meta(filas):
            p = fich("m%d" % len(os.listdir(tmp)), filas)
            m = os.path.join(tmp, "meta%d.json" % len(os.listdir(tmp)))
            _w(m, {"id": "gid://shopify/BulkOperation/1", "object_count": str(len(filas)), "file_size": str(os.path.getsize(p)),
                   "url": pathlib.Path(p).as_uri()})
            return m
        _w(os.path.join(tmp, "plan_almacenes.json"), {"tags": {O: {"remove": [], "add": ["MDP"]}}, "fos": {}})
        _w(os.path.join(tmp, "antes.json"), {O: ["D10"]})
        _w(os.path.join(tmp, "fos_estado.json"), {"cola": [], "i": 0, "ok": [], "rechazos": [], "abortado": None})
        tgs_ok = [{"id": O, "tags": ["D10", "MDP"]}]
        for args, filas, rc, txt in ((["verificar", "--hasta", "t2"], fifo, 5, "CONTENIDO AJENO (tags): línea 1"),
                                     (["verificar", "--hasta", "t2"], tgs_ok, 0, "OK: se puede seguir"),
                                     (["cerrar"], inv, 5, "CONTENIDO AJENO (pedidos): línea 1"),
                                     (["cerrar"], ped, 0, '"tags_pedidos"')):
            r = subprocess.run([sys.executable, os.path.abspath(__file__), *args, "--out", tmp, "--bulk", meta(filas)],
                               capture_output=True, text=True)
            ok(r.returncode == rc and txt in r.stdout, f"firma e2e {args} -> {r.returncode} {r.stdout[-300:]} {r.stderr[-300:]}")
    fallos += _selftest_engine()
    fallos += _selftest_v90()
    fallos += _selftest_v92()
    return fallos


def _selftest_v90():
    """v90 (06/10/2026, Bruno): (1) FRAUDE/CONTRACARGO fuera de scope como DESISTIMIENTO; (2) pedidos 100% despiece:
    RESERVA/SIN STOCK/tags de SKU se conservan (A.7); (3) criterio único de despiece. Corre engine.py + diff.py."""
    import importlib.util
    import tempfile
    fallos = []

    def ok(c, m):
        if not c:
            fallos.append("v90: " + m)
    sd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
    ep, dp = os.path.join(sd, "engine.py"), os.path.join(sd, "diff.py")
    if not (os.path.exists(ep) and os.path.exists(dp)):
        return [f"v90: faltan engine.py/diff.py en {sd} (§2.1 paso 3)"]
    spec = importlib.util.spec_from_file_location("nd_engine_selftest_v90", ep)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    # (3) criterio único: los 9 casos obligatorios (iguales en A.2, A.3 y A.7) + placeholder excluido antes
    for sku, tit, esp in (("PZ-0586-BANDEJA-MADERA", "x", True), ("ND-0586-BANDEJA-MADERA", "Bandeja Corina", True),
                          ("ND-0732-PATA-PIEZA-3", None, True), ("ND-0299-LIGHTBROWN", "Silla Maestro con Reposabrazos", False),
                          ("ND-0773-WALNUT", "Silla … Reposabrazos Isabella", False), ("", "PROTECTOR DE PATA - ND-0776-WALNUT -", True),
                          (None, "Despiece PZ-0604", True), ("", "Silla Isabella", False), (" pz-0080-rueda ", "", True)):
        ok(eng.is_despiece(sku, tit) == esp, f"is_despiece({sku!r}, {tit!r}) != {esp}")
    ok(eng.is_placeholder_line(None, "ENVIO DIVIDIDO #s1"), "ENVIO DIVIDIDO debe ser placeholder")

    n = [0]

    def ped(name, ts, tags, lineas, cerradas=(), fin="PAID"):
        """lineas/cerradas: [(sku, titulo, almacén)] en FOs OPEN / CLOSED (estas con remaining 0)."""
        n[0] += 1
        og = f"gid://shopify/Order/{n[0]}"
        filas = [{"id": og, "name": name, "createdAt": ts, "tags": list(tags), "closed": False, "displayFinancialStatus": fin,
                  "displayFulfillmentStatus": "UNFULFILLED", "customer": None, "sourceName": "web",
                  "totalPriceSet": {"shopMoney": {"amount": "0.00", "currencyCode": "EUR"}}, "shippingLine": None, "discountCodes": []}]
        for estado, rem, ls in (("OPEN", 1, lineas), ("CLOSED", 0, cerradas)):
            for sku, tit, loc in ls:
                n[0] += 1
                fg = f"gid://shopify/FulfillmentOrder/{n[0]}"
                filas.append({"id": fg, "status": estado, "assignedLocation": {"name": loc},
                              "deliveryMethod": {"methodType": "SHIPPING"}, "__parentId": og})
                filas.append({"id": f"gid://shopify/FulfillmentOrderLineItem/{n[0]}", "remainingQuantity": rem,
                              "lineItem": {"sku": sku, "name": tit}, "__parentId": fg})
        return filas

    def var(i, sku, locs):
        out = [{"id": f"gid://shopify/ProductVariant/{i}", "sku": sku, "product": {"id": f"gid://shopify/Product/{i}", "status": "ACTIVE"},
                "inventoryItem": {"id": f"gid://shopify/InventoryItem/{i}"}}]
        for loc, av, oh, cm in locs:
            out.append({"location": {"name": loc}, "quantities": [{"name": "available", "quantity": av}, {"name": "on_hand", "quantity": oh},
                                                                  {"name": "incoming", "quantity": 0}, {"name": "committed", "quantity": cm}],
                        "__parentId": f"gid://shopify/ProductVariant/{i}"})
        return out
    # ND-T: 1 unidad libre en D10; AP sobrevendido (-2). ND-M: sin stock en ningún sitio (rotura).
    inv = var(1, "ND-T", [("D10", 1, 1, 0), ("Almacén Principal", -2, 0, 2)]) + var(2, "ND-M", [("D10", 0, 0, 0)])

    def correr(filas):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "orders_raw.jsonl"), "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for x in filas))
            open(os.path.join(tmp, "inventory_raw.jsonl"), "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for x in inv))
            for sc in (ep, dp):
                r = subprocess.run([sys.executable, sc], cwd=tmp, capture_output=True, text=True)
                if r.returncode != 0:
                    return None, f"{os.path.basename(sc)}: {r.stdout[-300:]} {r.stderr[-300:]}"
            res = pickle.load(open(os.path.join(tmp, "scope.pkl"), "rb"))
            df = pickle.load(open(os.path.join(tmp, "diff.pkl"), "rb"))
        out = {x["order"]["name"]: {"g": x["group"], "add": set(), "rem": set(), "fo": 0} for x in res}
        for k, c in (("tagsAdd", "add"), ("tagsRemove", "rem")):
            for t in df[k]:
                out[t["order"]][c] |= set(t["tags"])
        for t in df["moves"] + df["splits"]:
            out[t["order"]]["fo"] += 1
        return out, None
    AP, D = "Almacén Principal", "D10"
    filas = (ped("#fraude", "2026-09-01T00:00:00Z", ["FRAUDE", "RESERVA", "ND-T"], [("ND-T", "t", AP)])
             + ped("#contracargo", "2026-09-02T00:00:00Z", ["contracargo"], [("ND-T", "t", AP)])
             + ped("#desist", "2026-09-03T00:00:00Z", ["DESISTIMIENTO", "RESERVA"], [("ND-T", "t", AP)])
             + ped("#normal", "2026-09-04T00:00:00Z", ["RESERVA", "ND-T"], [("ND-T", "t", AP)])
             + ped("#fraudealto", "2026-09-05T00:00:00Z", ["FRAUDE ALTO"], [("ND-M", "m", D)])
             + ped("#pz_reserva", "2026-09-06T00:00:00Z", ["RESERVA", "PZ-0586-BANDEJA-MADERA", "ND-X", "MDP"],
                   [("PZ-0586-BANDEJA-MADERA", "Bandeja", "Matadepera")], cerradas=[("ND-X", "x", "Matadepera")])
             + ped("#pz_sinstock", "2026-09-07T00:00:00Z", ["SIN STOCK", "DESPIECE"], [("", "PROTECTOR DE PATA - ND-0776-WALNUT -", D)])
             + ped("#pz_titulo", "2026-09-08T00:00:00Z", [], [(None, "Despiece PZ-0604", D)])
             + ped("#mixto", "2026-09-09T00:00:00Z", ["D10"], [("ND-M", "m", D), ("PZ-1", "p", D)])
             + ped("#silla", "2026-09-10T00:00:00Z", [], [("ND-0299-LIGHTBROWN", "Silla Maestro con Reposabrazos", D)]))
    r, err = correr(filas)
    if err:
        return fallos + [f"v90: e2e {err}"]
    nada = lambda p: not (r[p]["add"] or r[p]["rem"] or r[p]["fo"])
    ok(r["#fraude"]["g"] == "FRAUDE_CONTRACARGO" and nada("#fraude"), f"FRAUDE pagado con RESERVA no se toca {r['#fraude']}")
    ok(r["#contracargo"]["g"] == "FRAUDE_CONTRACARGO" and nada("#contracargo"), f"CONTRACARGO (minúsculas) no se toca {r['#contracargo']}")
    ok(r["#desist"]["g"] == "DESISTIMIENTO" and nada("#desist"), f"DESISTIMIENTO como antes {r['#desist']}")
    ok(r["#normal"]["g"] == "02_D10_ENTERO_SALE_YA" and r["#normal"]["rem"] == {"RESERVA", "ND-T"},
       f"FRAUDE/CONTRACARGO no consumen stock: el normal se queda la unidad de D10 {r['#normal']}")
    ok(r["#fraudealto"]["g"] != "FRAUDE_CONTRACARGO", f"'FRAUDE ALTO' no es tag de bloqueo de A.3 (no se amplía) {r['#fraudealto']}")
    ok(r["#pz_reserva"]["g"] == "09_DESPIECE" and r["#pz_reserva"]["add"] == {"DESPIECE"} and not r["#pz_reserva"]["rem"]
       and not r["#pz_reserva"]["fo"], f"100% despiece: RESERVA/SKU/almacén se conservan, solo +DESPIECE {r['#pz_reserva']}")
    ok(r["#pz_sinstock"]["g"] == "09_DESPIECE" and nada("#pz_sinstock"), f"100% despiece con SIN STOCK se conserva {r['#pz_sinstock']}")
    ok(r["#pz_titulo"]["g"] == "09_DESPIECE" and r["#pz_titulo"]["add"] == {"DESPIECE"} and not r["#pz_titulo"]["rem"],
       f"sin SKU y título 'Despiece PZ-…' es despiece {r['#pz_titulo']}")
    ok(r["#mixto"]["g"] == "09_DESPIECE" and r["#mixto"]["add"] == {"DESPIECE", "RESERVA", "SIN STOCK", "ND-M"}
       and r["#mixto"]["rem"] == {"D10"}, f"mixto como antes (RESERVA/SIN STOCK/SKU por su parte normal) {r['#mixto']}")
    ok(r["#silla"]["g"] != "09_DESPIECE" and "DESPIECE" not in r["#silla"]["add"], f"silla con SKU real y 'Reposabrazos' no es despiece {r['#silla']}")
    return fallos


def _selftest_engine():
    """v89: prioridad Repo > B2B Partnership > resto en engine.py (../scripts/engine.py junto a este script)."""
    import importlib.util
    import tempfile
    fallos = []

    def ok(c, m):
        if not c:
            fallos.append("engine: " + m)
    ep = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "engine.py")
    if not os.path.exists(ep):
        return [f"engine: no está {ep} (§2.1 paso 3)"]
    spec = importlib.util.spec_from_file_location("nd_engine_selftest", ep)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    for t, esp in (("Repo rotura s141353", True), ("  REPOSICIÓN tablero", True), ("Reposicion", True), ("repo", True),
                   ("REPO-1", True), ("repo_x", True), ("Repo2", True), ("Reportaje", False), ("report", False),
                   ("REPORTE", False), ("reposo", False), ("repositorio", False), ("Envío estándar", False), ("", False), (None, False)):
        ok(eng.es_texto_repo(t) == esp, f"es_texto_repo({t!r}) != {esp}")
    ok(eng.es_repo({"shipping_title": "Repo rotura s1", "discount_codes": []}), "repo por shippingLine")
    ok(eng.es_repo({"shipping_title": None, "discount_codes": ["X", "REPOSICION-X"]}), "repo por discountCodes")
    ok(not eng.es_repo({"shipping_title": "Reportaje", "discount_codes": ["REPORT10"]}), "Reportaje/REPORT10 no es repo")

    def orden(gid, name, ts, tags=(), envio=None, codigos=()):
        return {"id": f"gid://shopify/Order/{gid}", "name": name, "createdAt": ts, "tags": list(tags), "closed": False,
                "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED", "customer": None, "sourceName": "web",
                "totalPriceSet": {"shopMoney": {"amount": "0.00", "currencyCode": "EUR"}},
                "shippingLine": None if envio is None else {"title": envio}, "discountCodes": list(codigos)}

    def fo(gid, ogid, loc="Almacén Principal"):
        return [{"id": f"gid://shopify/FulfillmentOrder/{gid}", "status": "OPEN", "assignedLocation": {"name": loc},
                 "deliveryMethod": {"methodType": "SHIPPING"}, "__parentId": f"gid://shopify/Order/{ogid}"},
                {"id": f"gid://shopify/FulfillmentOrderLineItem/{gid}", "remainingQuantity": 1,
                 "lineItem": {"sku": "ND-T", "name": "t"}, "__parentId": f"gid://shopify/FulfillmentOrder/{gid}"}]
    # 1 unidad de ND-T en D10 (colchón 0), nada libre en propios ni incoming: el primero procesado se la queda.
    inv = [{"id": "gid://shopify/ProductVariant/1", "sku": "ND-T", "product": {"id": "gid://shopify/Product/1", "status": "ACTIVE"},
            "inventoryItem": {"id": "gid://shopify/InventoryItem/1"}},
           {"location": {"name": "D10"}, "quantities": [{"name": "available", "quantity": 1}, {"name": "on_hand", "quantity": 1},
                                                        {"name": "incoming", "quantity": 0}, {"name": "committed", "quantity": 0}],
            "__parentId": "gid://shopify/ProductVariant/1"},
           # Los dos pedidos están comprometidos en AP sin stock (available -2): hysteresis rota, compiten por D10.
           {"location": {"name": "Almacén Principal"}, "quantities": [{"name": "available", "quantity": -2}, {"name": "on_hand", "quantity": 0},
                                                                      {"name": "incoming", "quantity": 0}, {"name": "committed", "quantity": 2}],
            "__parentId": "gid://shopify/ProductVariant/1"}]

    def correr(pedidos):
        with tempfile.TemporaryDirectory() as tmp:
            filas = []
            for k, o in enumerate(pedidos, 1):
                filas += [o] + fo(k, o["id"].split("/")[-1])
            open(os.path.join(tmp, "orders_raw.jsonl"), "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for x in filas))
            open(os.path.join(tmp, "inventory_raw.jsonl"), "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for x in inv))
            r = subprocess.run([sys.executable, ep], cwd=tmp, capture_output=True, text=True)
            if r.returncode != 0:
                return {"error": r.stdout[-300:] + r.stderr[-300:]}
            res = pickle.load(open(os.path.join(tmp, "scope.pkl"), "rb"))
            return {x["order"]["name"]: x["group"] for x in res}
    viejo = orden(1, "#viejo", "2026-09-01T00:00:00Z")
    b2b = orden(2, "#b2b", "2026-09-02T00:00:00Z", tags=["B2B Partnership"])
    r = correr([viejo, orden(3, "#repo", "2026-10-05T00:00:00Z", envio="Repo rotura s1")])
    ok(r.get("#repo") == "02_D10_ENTERO_SALE_YA" and r.get("#viejo") == "05_ROTURA", f"repo nueva vs normal antiguo {r}")
    r = correr([b2b, orden(3, "#repo", "2026-10-05T00:00:00Z", codigos=["REPOSICION-X"])])
    ok(r.get("#repo") == "02_D10_ENTERO_SALE_YA" and r.get("#b2b") == "05_ROTURA", f"repo (discountCodes) vs B2B {r}")
    r = correr([viejo, orden(3, "#rep", "2026-10-05T00:00:00Z", envio="Reportaje", codigos=["REPORT10"])])
    ok(r.get("#viejo") == "02_D10_ENTERO_SALE_YA" and r.get("#rep") == "05_ROTURA", f"Reportaje no es repo {r}")
    r = correr([viejo, b2b])
    ok(r.get("#b2b") == "02_D10_ENTERO_SALE_YA" and r.get("#viejo") == "05_ROTURA", f"B2B sigue por delante del resto {r}")
    r = correr([orden(4, "#repo_nueva", "2026-10-05T00:00:00Z", envio="repo"),
                orden(5, "#repo_vieja", "2026-10-01T00:00:00Z", envio="REPOSICIÓN")])
    ok(r.get("#repo_vieja") == "02_D10_ENTERO_SALE_YA" and r.get("#repo_nueva") == "05_ROTURA", f"entre repos, por createdAt {r}")
    return fallos



def _selftest_v92():
    """v92 (06/10/2026, Bruno, caso #s138524): juntar_en_d10 de engine.py. Un pedido que espera algo en D10 pasa a D10
    las lineas locales que tengan stock LIBRE en D10 (lo que sobra tras el reparto), todas o las que se pueda."""
    import importlib.util
    fallos = []

    def ok(c, m):
        if not c:
            fallos.append("v92: " + m)
    ep = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "engine.py")
    if not os.path.exists(ep):
        return [f"v92: no está {ep} (§2.1 paso 3)"]
    spec = importlib.util.spec_from_file_location("nd_engine_selftest_v92", ep)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    if not hasattr(eng, "juntar_en_d10"):
        return ["v92: engine.py no tiene juntar_en_d10 (¿engine viejo?)"]

    def dec(sku, motivo, destino, qty=1, loc=None):
        return {"sku": sku, "name": sku, "qty": qty, "fo_id": "fo-" + sku, "fo_location": loc or destino,
                "line_id": "l-" + sku, "motivo": motivo, "destino": destino}

    def ped(name, ds, desp=None):
        return {"group": eng.agrupar(ds, desp or []), "skip": False, "decisions": ds, "despiece_lines": desp or [],
                "order": {"name": name}}
    res = [
        ped("#s138524", [dec("SILLA", "stock local", "Terrassa", 2), dec("MESA", "llega a D10", "D10")]),
        ped("#parcial", [dec("A", "stock local", "Matadepera"), dec("B", "stock local", "Terrassa"), dec("C", "cola", "D10")]),
        ped("#rotura", [dec("A2", "stock local", "Terrassa"), dec("R", "rotura", "D10")]),
        ped("#despiece", [dec("A3", "stock local", "Terrassa"), dec("M", "llega a D10", "D10")], desp=[{"sku": "PZ-1"}]),
        ped("#sale_ya", [dec("A4", "stock local", "Terrassa"), dec("D", "stock D10", "D10")]),
        ped("#viejo", [dec("X", "stock local", "Terrassa"), dec("W", "llega a D10", "D10")]),
        ped("#nuevo", [dec("X", "stock local", "Matadepera"), dec("W", "llega a D10", "D10")]),
    ]
    ok(res[0]["group"] == "01_SPLIT_B", f"agrupar sigue igual: {res[0]['group']}")
    libre = {"SILLA": 392, "A": 1, "B": 0, "A2": 5, "A3": 5, "A4": 5, "X": 1}
    j = eng.juntar_en_d10(res, libre)
    g = {r["order"]["name"]: r for r in res}
    d = lambda n: [(x["sku"], x["destino"], x["motivo"]) for x in g[n]["decisions"]]
    ok(d("#s138524")[0] == ("SILLA", "D10", "stock D10") and g["#s138524"]["group"] == "03_D10_ENTERO_ESPERA_RESERVA",
       f"#s138524 entero a D10: {d('#s138524')} {g['#s138524']['group']}")
    ok(libre["SILLA"] == 390, f"consume el D10 libre: {libre['SILLA']}")
    ok(d("#parcial")[0][1] == "D10" and d("#parcial")[1][1] == "Terrassa" and g["#parcial"]["group"] == "01_SPLIT_C",
       f"parcial: mueve lo que se puede {d('#parcial')} {g['#parcial']['group']}")
    ok(d("#rotura")[0][1] == "Terrassa" and libre["A2"] == 5, "rotura (SPLIT_D) no se toca")
    ok(d("#despiece")[0][1] == "Terrassa" and libre["A3"] == 5, "pedido con despiece no se toca (A.7)")
    ok(d("#sale_ya")[0][1] == "Terrassa" and libre["A4"] == 5, "nada esperando: no se junta")
    ok(d("#viejo")[0][1] == "D10" and d("#nuevo")[0][1] == "Matadepera", "el D10 libre va al primero en el orden del reparto")
    ok(len(j) == 3 and "kept" not in g["#s138524"]["decisions"][0], f"juntados: {[x[0] for x in j]}")
    return fallos

def cmd_selftest(_):
    f = _selftest()
    if f:
        print("SELFTEST FALLIDO — NO seguir.")
        for x in f:
            print("   ", x)
        raise SystemExit(1)
    print("SELFTEST OK")
    print(f"SHA256 canonico nd_almacenes_composio.py: {hash_canonico()}")


COMANDOS = {"selftest": cmd_selftest, "preparar": cmd_preparar, "verificar": cmd_verificar,
            "siguiente": cmd_siguiente, "lote": cmd_lote, "cerrar": cmd_cerrar}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMANDOS:
        print("Uso: python3 nd_almacenes_composio.py {" + "|".join(COMANDOS) + "} [args]")
        raise SystemExit(2)
    try:
        COMANDOS[sys.argv[1]](sys.argv[2:])
    except BulkAjena as e:
        print(f"ERROR: {e}")
        raise SystemExit(5)
    except NDError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
