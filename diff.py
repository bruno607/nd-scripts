import pickle, json
from collections import defaultdict

with open("scope.pkl", "rb") as f:
    results = pickle.load(f)

LOC_IDS = {
    "Almacén Principal": "gid://shopify/Location/58556219571",
    "D10": "gid://shopify/Location/111265644930",  # ex "DHL" — Location renombrada 08/09/2026, mismo GID
    "Matadepera": "gid://shopify/Location/111219868034",
    "Terrassa": "gid://shopify/Location/104513241474",
}

# ALIAS DE ALMACEN "DHL" -> "D10" (v51, 08/09/2026, aviso de Northdeco): mismo cambio que en
# engine.py — Location renombrada in-place (mismo GID) + retag masivo de pedidos abiertos
# DHL->D10, hoy a la tarde/noche. `engine.py` ya normaliza cualquier "DHL" leido de Shopify a
# "D10" (ver `normalize_loc_name` ahi), asi que las `decisions` que llegan aca desde
# `scope.pkl` ya usan "D10" como destino/motivo siempre. Este archivo, ademas, sigue
# reconociendo el tag TEXTUAL "DHL" en `ALL_WH_TAGS` y `REVERSE_WAREHOUSE_TAG" — no porque
# haga falta para pedidos nuevos (el motor ya solo agrega "D10"), sino para AUTO-LIMPIAR
# cualquier pedido que todavia tenga el tag viejo "DHL" puesto (por ejemplo si el retag
# masivo de Northdeco no llega a cubrir el 100% de los pedidos abiertos): al recalcular
# `new_tags`, se le saca "DHL" igual que cualquier otro tag de almacen viejo y se le pone el
# "D10" que corresponda — autocorrectivo, sin necesitar una corrida especial de migracion.
WAREHOUSE_TAG = {"Terrassa": "TERRASA", "Matadepera": "MDP", "Almacén Principal": "AP"}
# Inverso de WAREHOUSE_TAG + D10 (que no tiene traduccion, el tag ES el nombre de la ubicacion).
# Se usa para saber a que ubicacion fisica mover el despiece cuando el pedido resuelve en un tag
# de almacen concreto (ver bloque "Despiece asociado al mismo almacen..." mas abajo). Se
# mantiene la entrada "DHL" -> "D10" (no "DHL" -> "DHL") para que, si por algun motivo un
# pedido todavia trae el tag viejo "DHL" en el momento de calcular el despiece asociado, el
# despiece se mueva al almacen fisico CORRECTO ("D10"), no a un nombre de ubicacion que ya no
# existe.
REVERSE_WAREHOUSE_TAG = {"TERRASA": "Terrassa", "MDP": "Matadepera", "AP": "Almacén Principal", "DHL": "D10", "D10": "D10"}
ALL_WH_TAGS = {"TERRASA", "MDP", "AP", "DHL", "D10"}

# TAG "DIVIDIDO" (18/09/2026, pedido explicito de Northdeco): marca los pedidos que salen
# repartidos entre DOS O MAS almacenes distintos, ADEMAS de su tag de almacen habitual
# (D10 / MDP / TERRASA / AP). El problema que resuelve: un pedido que sale de Matadepera +
# Terrassa hoy lleva un unico tag "MDP" (ver la regla 3 de abajo, que colapsa cualquier
# combinacion de almacenes propios a un solo tag), asi que en el picking no hay forma de
# saber que hay una parte que sale de otro sitio.
#
# ALCANCE, confirmado por Northdeco: SOLO pedidos "por salir", es decir con TODAS sus
# lineas normales ya resueltas (nada en "llega a D10" / "cola" / "rotura"). Un pedido
# repartido pero con algo esperando stock NO lleva DIVIDIDO — de hecho hoy no lleva
# tampoco tag de almacen, lleva RESERVA + el SKU que espera, y ese es el estado que
# manda. El tag aparece recien cuando el pedido queda listo para salir de verdad.
# Medido sobre el scope del 18/09: ~12 pedidos lo llevarian, contra 432 que estan
# repartidos en el plan pero con algo esperando (esos quedan fuera a proposito).
#
# Se calcula sobre TODOS los destinos de `decisions` (no solo `destinos_local`): un
# reparto D10 + Terrassa ya resuelto cuenta como dividido igual que Matadepera +
# Terrassa, aunque el primero se tagee D10 por la regla 1 y el segundo MDP por la 3.
#
# Nota de alcance: se mira solo la parte NORMAL del pedido (`decisions`). Un pedido cuyo
# producto entero sale de un almacen y su pieza de despiece de otro NO cuenta como
# dividido aca — para ese caso ya existe el tag DESPIECE, que es el que avisa en el
# picking que hay una pieza suelta de por medio.
SPLIT_TAG = "DIVIDIDO"

# TAG "ENVIO PARCIAL" (23/09/2026, v68, decision de Northdeco): marca los pedidos que YA
# tuvieron una parte enviada (Shopify: displayFulfillmentStatus == PARTIALLY_FULFILLED,
# "Parcialmente preparado" en el admin) y cuyo RESTO ya esta asignado a un almacen, es
# decir que el pedido termina con un tag de almacen (D10 / MDP / TERRASA / AP). Se pone
# ADEMAS del tag de almacen, igual que DIVIDIDO.
#
# DUENO DEL TAG: este protocolo (confirmado por Northdeco el 23/09). Lo pone y lo saca
# segun el estado real, en cada corrida, como DIVIDIDO. Consecuencias aceptadas:
#   - Un pedido parcialmente preparado cuyo resto sigue esperando (RESERVA / SIN STOCK)
#     NO lleva ENVIO PARCIAL, y si alguien se lo puso a mano, se le saca. Cuando el resto
#     quede asignado a un almacen, el tag vuelve solo.
#   - 10_TIENDA y pedidos 100% despiece no llevan tag de almacen -> tampoco ENVIO PARCIAL.
#   - Pedidos fuera de scope (NO_PENDING, 11_NO_PAGADO, ARCHIVADO, DESISTIMIENTO, FRAUDE_CONTRACARGO) y
#     01_SPLIT_D no se tocan: si tienen el tag, lo conservan (mismo criterio que DIVIDIDO).
#
# POR QUE displayFulfillmentStatus Y NO "hay alguna FO CLOSED": una FO queda CLOSED
# tambien cuando se mueve o se parte (fulfillmentOrderMove/Split), cosa que este mismo
# protocolo hace todas las noches. Usar CLOSED marcaria como envio parcial pedidos que
# nunca enviaron nada. displayFulfillmentStatus lo calcula Shopify sobre lo enviado real.
#
# FAIL-SAFE: si el pedido no trae fulfillment_status (query de extraccion sin el campo),
# el tag NO se toca en ningun sentido. Sin esto, una query vieja lo borraria de todos.
PARTIAL_TAG = "ENVIO PARCIAL"
PARTIAL_STATUS = "PARTIALLY_FULFILLED"
# Tags de automatizaciones/apps (wholesale, Shop app) que no pintan nada en pedidos normales de
# Northdeco — se quitan siempre que aparezcan, en cualquier grupo que se procese. Ampliado 26/08
# noche: Northdeco señaló "Login with Shop" y "Shop" (tags que pone la app Shop de Shopify al
# margen del pedido en si) igual de irrelevantes que los de wholesale — mismo tratamiento, se
# retiran sin excepcion.
WHOLESALE_TAGS = {"WholesaleHelperRegularOrderGraphQL", "wholesale", "Login with Shop", "Shop"}

# LIMITE DE LONGITUD DE TAG DE ORDER (26/09/2026, decision de Northdeco tras #s144861):
# Shopify rechaza cualquier tag de Order de mas de ~40 caracteres ("Order tags no es
# valido"). Los SKUs compuestos, patron "X // Y" (venta en par/conjunto — ver
# ND-0630-BLACKEDITION // ND-0994-BLACKEDITION, 44 caracteres, rechazado el 25/09 y de
# nuevo el 26/09 en #s144861), chocan con ese limite cuando los dos componentes juntos
# superan el limite. Decision de Northdeco (26/09/2026, en vivo, sobre #s144861): el
# pedido "deberia tener tag del producto, no importa cuantos caracteres tiene" — es decir,
# SIEMPRE tiene que quedar trackeado por SKU mientras espera, nunca solo con `RESERVA` a
# secas. La solucion tecnica: en vez de UN tag con el SKU compuesto entero ("X // Y"), se
# usan DOS tags, uno por cada componente ("X" y "Y" por separado) — cada componente solo
# nunca se acerco al limite en ningun SKU visto del catalogo. Se aplica siempre que el SKU
# tenga el separador " // ", no solo cuando el compuesto entero supera el limite (mismo
# criterio para todos, evita depender de que la suma de longitudes quede corta por
# casualidad como pasaba con ND-0648-VINTAGE // ND-0997).
MAX_ORDER_TAG_LEN = 40

def _sku_tag_parts(sku):
    """Devuelve el/los tag(s) de SKU a usar para trackear una linea en RESERVA, y tambien
    el conjunto de tags 'propios de este SKU' a reconocer para limpieza (ver
    all_order_skus mas abajo). Para un SKU compuesto "X // Y" devuelve {X, Y} (los
    componentes, lo que se tageará de ahora en más) MAS el string compuesto completo (para
    poder limpiar el tag viejo de una corrida anterior a este cambio, si quedó puesto).
    Para un SKU simple devuelve {sku} nada más. Cada parte se recorta a
    MAX_ORDER_TAG_LEN caracteres como red de seguridad final — no se vio ningún caso así
    en el catálogo, pero un tag nunca debe intentarse sin control de longitud."""
    if not sku:
        return set()
    parts = {sku}
    if " // " in sku:
        parts |= {p.strip() for p in sku.split(" // ") if p.strip()}
    return {p[:MAX_ORDER_TAG_LEN] for p in parts}

def _sku_tags_for_tracking(sku):
    """Los tags que hay que AGREGAR para trackear un SKU que se está esperando: los
    componentes de un SKU compuesto por separado (nunca el string compuesto entero, que es
    el que choca con el límite de Shopify)."""
    if not sku:
        return set()
    if " // " in sku:
        return {p.strip()[:MAX_ORDER_TAG_LEN] for p in sku.split(" // ") if p.strip()}
    return {sku[:MAX_ORDER_TAG_LEN]}

def _desired_tags_base(r):
    group = r["group"]
    order = r["order"]
    tags = set(order["tags"])
    new_tags = set(tags)
    # remove all warehouse-type tags first, then set correct one(s)
    new_tags -= ALL_WH_TAGS
    new_tags -= WHOLESALE_TAGS
    new_tags.discard(SPLIT_TAG)
    # Regla real de DESPIECE (corregida 26/08 noche, cuarta revision, tras reporte
    # directo de Northdeco): el tag NO depende de si el pedido entero clasifica en
    # el grupo 09_DESPIECE (eso solo pasa cuando TODAS las lineas son despiece).
    # Depende de si el pedido tiene AL MENOS UNA linea despiece, aunque el resto de
    # lineas sean normales y el pedido este repartido en varios fulfillment orders
    # (ej.: un mueble entero que va a D10 + su bandeja de despiece que sale de
    # Terrassa via "REPO PEDIDO #..." — Northdeco quiere el tag DESPIECE en ese
    # pedido igual, para saber en el picking que hay una pieza suelta de por medio).
    # `despiece_lines` (calculado en engine.py con is_despiece linea a linea, no por
    # grupo) es la fuente de verdad; el grupo 09_DESPIECE es solo el caso particular
    # en que TODAS las lineas caen ahi.
    has_despiece_line = bool(r.get("despiece_lines"))
    if has_despiece_line:
        new_tags.add("DESPIECE")
    else:
        new_tags.discard("DESPIECE")

    decisions = r.get("decisions", [])

    if group == "10_TIENDA":
        new_tags.add("TIENDA")
        return new_tags, set(), set()  # (tags, sku_tags_needed, unused)
    if group == "09_DESPIECE" and not decisions:
        # Pedido 100% despiece, sin ninguna linea normal que decidir -> con el tag DESPIECE
        # (ya puesto arriba); RESERVA/SIN STOCK/SKU se conservan (v90, ver abajo).
        # v83 (05/10/2026, decision de Bruno): el tag de ALMACEN de un pedido 100% despiece lo
        # gestiona quien lleva los despieces (el 05/10 se etiquetaron a mano los 98 que no lo
        # tenian). Este protocolo lo CONSERVA tal cual: no lo quita ni lo pone. Antes de v83 se
        # quitaba (la limpieza de ALL_WH_TAGS de arriba) y la corrida deshacia ese etiquetado.
        new_tags |= (tags & ALL_WH_TAGS)
        # v90 (06/10/2026, decision de Bruno): RESERVA, SIN STOCK y los tags de SKU de un pedido
        # 100% despiece los gestiona A.7 (DESPIECES). Este protocolo ya NO los pone ni los quita:
        # se conservan tal cual (new_tags parte de los tags actuales; el barrido de tags de SKU se
        # salta mas abajo, ver es_100_despiece). Antes de v90 se quitaban RESERVA/SIN STOCK aqui.
        return new_tags, set(), set()

    # Pedido MIXTO reclasificado a "09_DESPIECE" para reporte (tiene decisions de su
    # parte normal, ver engine.py) o cualquier otro grupo normal: se calculan los tags
    # de almacen/RESERVA/SIN STOCK igual que siempre, a partir de `decisions` — el
    # grupo "09_DESPIECE" ya no implica saltarse esto cuando hay lineas normales.
    waiting = [d for d in decisions if d["motivo"] in ("llega a D10", "cola", "rotura")]
    sku_tags_needed = set()
    for d in waiting:
        sku_tags_needed |= _sku_tags_for_tracking(d.get("sku"))
    if waiting:
        new_tags.add("RESERVA")
    else:
        new_tags.discard("RESERVA")
    if any(d["motivo"] == "rotura" for d in decisions):
        new_tags.add("SIN STOCK")
    else:
        new_tags.discard("SIN STOCK")

    # Regla real (verificada contra PLAN v22, corrida del 25/08 ya cerrada; corregida
    # 29/08 tras reporte de Northdeco sobre #s121983 — ver punto 3, y vuelta a acotar el
    # mismo dia tras una primera correccion demasiado amplia que Northdeco corrigio):
    # 1) Si el pedido tiene alguna linea con destino D10 y nada queda esperando -> tag D10
    #    (aplica tanto a 02 D10 entero como a un SPLIT A ya resuelto del todo). Esta regla
    #    tiene prioridad incluso si ademas participan los 3 almacenes propios (si hay D10
    #    y no queda nada esperando, el pedido sale y se tagea D10 igual).
    # 2) Si no, y el pedido sale entero de un unico almacen propio -> su tag (TERRASA/MDP/AP).
    # 3) Si no, y son varios almacenes propios (split entre propios, sin D10) -> tag MDP fijo
    #    EN TODOS LOS CASOS, con una unica excepcion: cuando el split es EXACTAMENTE entre
    #    Almacen Principal y Terrassa (nada mas) -> tag TERRASA. Cualquier otra combinacion
    #    de 2+ almacenes propios (Terrassa+Matadepera, o los 3 juntos, o Matadepera+AP) ->
    #    tag MDP, igual que la regla original del 24/08.
    #    Corregido 29/08 (dos vueltas el mismo dia): primero se penso que Terrassa ganaba
    #    en cualquier split de 2 almacenes propios que la incluyera (eso tageo mal a
    #    #s140024 y a 5 pedidos historicos de Terrassa+Matadepera, ver #s118036/#s124844/
    #    #s129736/#s132711/#s133410 — se revirtieron todos a MDP). La regla real, confirmada
    #    por Northdeco, es mas angosta: SOLO el combo AP+Terrassa (sin Matadepera de por
    #    medio) usa tag TERRASA; Terrassa+Matadepera sigue siendo MDP como siempre.
    #    Esto aplica siempre que sea "para sacar el pedido" (decisions con motivo
    #    "stock local" unicamente, sin nada esperando) — si hay algo esperando el pedido
    #    no tiene tag de almacen de todos modos (ver rama `else` mas abajo, sin cambios).
    #
    # IMPORTANTE (v21, quinta revision): esto se calcula a partir de `motivos`/
    # `destinos_local` derivados de `decisions`, NO del string `group` — desde que un
    # pedido mixto (parte normal + parte despiece) se reclasifica como "09_DESPIECE"
    # para reporte (ver engine.py), el nombre del grupo ya no identifica de forma
    # fiable a que almacen(es) fue su parte normal. `decisions` sigue siendo la fuente
    # de verdad de a donde va cada linea normal, este o no el pedido en "09_DESPIECE".
    #
    # v51 (08/09/2026): el destino que emite engine.py para este almacen es siempre
    # "D10" (nunca mas "DHL", ver normalize_loc_name/alias en engine.py) — esta
    # comparacion y el tag que se agrega mas abajo usan "D10" en consecuencia.
    motivos = set(d["motivo"] for d in decisions)
    destinos_local = set(d["destino"] for d in decisions if d["motivo"] == "stock local")
    has_d10_destino = any(d.get("destino") == "D10" for d in decisions)
    if has_d10_destino and not waiting:
        new_tags.add("D10")
    elif motivos == {"stock local"} and len(destinos_local) == 1:
        w = next(iter(destinos_local))
        new_tags.add(WAREHOUSE_TAG[w])
    elif motivos == {"stock local"} and len(destinos_local) > 1:
        if destinos_local == {"Almacén Principal", "Terrassa"}:
            new_tags.add("TERRASA")
        else:
            new_tags.add("MDP")

    # DIVIDIDO: pedido por salir (nada esperando) cuyas lineas normales se reparten
    # entre 2+ destinos distintos. Ver el comentario de SPLIT_TAG arriba.
    destinos_todos = set(d["destino"] for d in decisions)
    if decisions and not waiting and len(destinos_todos) > 1:
        new_tags.add(SPLIT_TAG)

    return new_tags, sku_tags_needed, waiting

def desired_tags(r):
    # Envoltorio (v68): calcula los tags base y despues decide ENVIO PARCIAL sobre el
    # resultado FINAL, porque la condicion es "termina con tag de almacen". Ver el
    # comentario de PARTIAL_TAG arriba.
    new_tags, sku_tags_needed, waiting = _desired_tags_base(r)
    fstatus = r["order"].get("fulfillment_status")
    if r["group"] == "09_DESPIECE" and not r.get("decisions"):
        # v83: en un pedido 100% despiece el tag de almacen es ajeno (ver _desired_tags_base), asi que
        # ENVIO PARCIAL tampoco se toca: se conserva como este.
        if PARTIAL_TAG in set(r["order"]["tags"]):
            new_tags.add(PARTIAL_TAG)
        else:
            new_tags.discard(PARTIAL_TAG)
        return new_tags, sku_tags_needed, waiting
    if fstatus is None:
        return new_tags, sku_tags_needed, waiting  # fail-safe: no tocar ENVIO PARCIAL
    new_tags.discard(PARTIAL_TAG)
    if fstatus == PARTIAL_STATUS and (new_tags & ALL_WH_TAGS):
        new_tags.add(PARTIAL_TAG)
    return new_tags, sku_tags_needed, waiting

ops_moves = []
ops_tagsAdd = []
ops_tagsRemove = []
splits_needed = []
group_diff_counts = defaultdict(lambda: {"orders_total":0, "orders_changed":0, "moves":0, "splits":0, "tag_changes":0, "despiece_moves":0})

def es_100_despiece(r):
    # Pedido 100% despiece: grupo 09_DESPIECE sin ninguna linea normal que decidir.
    return r["group"] == "09_DESPIECE" and not r.get("decisions")

# v90: FRAUDE_CONTRACARGO (tag FRAUDE o CONTRACARGO) fuera de scope, igual que DESISTIMIENTO.
FUERA_DE_SCOPE = ("DESISTIMIENTO", "FRAUDE_CONTRACARGO", "NO_PENDING", "ARCHIVADO", "11_NO_PAGADO")

for r in results:
    group = r["group"]
    if group in FUERA_DE_SCOPE:
        continue
    order = r["order"]
    gd = group_diff_counts[group]
    gd["orders_total"] += 1
    changed = False

    if group == "01_SPLIT_D":
        continue  # never touch

    new_tags, sku_tags_needed, waiting = desired_tags(r)
    old_tags = set(order["tags"])
    decisions = r.get("decisions", [])

    # sku tags: remove any tag that matches one de los SKUs reales de ESTE pedido (de
    # CUALQUIERA de sus fulfillment order lines, pendientes o ya cerradas) que ya no
    # haga falta como tag de espera. Corregido 29/08 (tras reporte de Northdeco sobre
    # #s118224/#s121983): antes se detectaba con un patron generico ("empieza con
    # ND-"), que fallaba para SKUs de nomenclatura vieja/legacy sin ese prefijo (ej.
    # "OSAKA(076)-AZULOSCURO", producto "Sillon Reclinable Osaka") — el tag quedaba
    # pegado al pedido para siempre porque nunca calzaba con el patron "ND-". Un
    # primer intento de arreglo comparo solo contra `decisions` (lineas normales AUN
    # pendientes), pero eso genero una regresion: #s128886 tenia el tag
    # "ND-0770-OFFWHITE" de una linea que YA SE DESPACHO del todo (su fulfillment
    # order quedo CLOSED, remainingQuantity 0) — esa linea ya no aparece en
    # `decisions` (que solo trae lineas con remainingQuantity>0), asi que el tag
    # quedaba sin limpiar igual. La fuente de verdad correcta es TODAS las lineas de
    # TODOS los fulfillment orders del pedido (`order["fos"]`, incluidos los CLOSED),
    # sin importar si siguen pendientes o no — si el tag coincide con cualquier SKU
    # real de este pedido y no esta en `sku_tags_needed` (linea que sigue esperando
    # de verdad), se quita. Sin riesgo de falsos positivos: solo se tocan tags que
    # coinciden con un SKU que el pedido realmente tuvo en alguna de sus lineas.
    #
    # EXCEPCION: los SKUs de lineas de DESPIECE (`despiece_lines`, prefijo tipico
    # "PZ-") se excluyen de este barrido — no son tags de espera/RESERVA, son
    # marcadores fijos de picking (senalan que el pedido tiene una pieza suelta con
    # ese SKU) y deben quedar puestos sin importar el estado de la linea normal. Sin
    # esta exclusion, el barrido ampliado de mas arriba los detectaba como "sku real
    # del pedido no necesario" y los borraba por error (ver #s140004/#s140017,
    # detectado 29/08 al validar el fix anterior).
    despiece_skus = set(d["sku"] for d in r.get("despiece_lines", []) if d.get("sku"))
    all_order_skus = set()
    for fo in order["fos"]:
        for l in fo["lines"]:
            sku = l.get("sku")
            if not sku or sku in despiece_skus:
                continue
            # _sku_tag_parts trae el SKU crudo (limpia el tag viejo con "X // Y" entero,
            # de antes del 26/09) MAS sus componentes por separado (limpia los tags
            # nuevos, uno por componente, el dia que la espera se resuelva).
            all_order_skus |= _sku_tag_parts(sku)
    # v90: en un pedido 100% despiece los tags de SKU son de A.7 -> no se barren (se conservan).
    sku_like_old = set() if es_100_despiece(r) else (old_tags & all_order_skus)
    final_tags = new_tags - sku_like_old
    final_tags |= sku_tags_needed

    to_add = final_tags - old_tags
    to_remove = old_tags - final_tags
    if to_add or to_remove:
        changed = True
        gd["tag_changes"] += 1
        if to_add:
            ops_tagsAdd.append({"order": order["name"], "id": order["id"], "tags": sorted(to_add)})
        if to_remove:
            ops_tagsRemove.append({"order": order["name"], "id": order["id"], "tags": sorted(to_remove)})

    # "10_TIENDA" nunca tiene decisions (pickup, no se reparte). "09_DESPIECE" solo se
    # salta el reparto de almacen si NO tiene decisions (100% despiece) — si las tiene
    # (pedido mixto reclasificado aqui para reporte, ver engine.py), su parte normal
    # sigue necesitando el mismo procesamiento de moves/splits que cualquier otro grupo.
    if group == "10_TIENDA" or (group == "09_DESPIECE" and not decisions):
        if changed:
            gd["orders_changed"] += 1
        continue

    # group FO -> set of desired destinos among its normal lines
    fo_destinos = defaultdict(set)
    fo_loc = {}
    for d in decisions:
        fo_destinos[d["fo_id"]].add(d["destino"])
        fo_loc[d["fo_id"]] = d["fo_location"]

    needs_split = any(len(v) > 1 for v in fo_destinos.values())
    if needs_split:
        gd["splits"] += 1
        changed = True
        splits_needed.append({"order": order["name"], "id": order["id"], "group": group,
                               "decisions": decisions})
    else:
        for fo_id, destinos in fo_destinos.items():
            destino = next(iter(destinos))
            if fo_loc[fo_id] != destino:
                gd["moves"] += 1
                changed = True
                ops_moves.append({"order": order["name"], "fo_id": fo_id, "from": fo_loc[fo_id], "to": destino})

    # Despiece asociado al MISMO almacen que el producto normal (decision de Northdeco,
    # 04/09/2026, tras revisar #s126036: la Corina resolvia en D10 pero su despiece
    # PZ-0586-BANDEJA-MADERA habia quedado tageado en Terrassa — no coincidian). Regla:
    # fisicamente se desarma la pieza entera de donde ESTA esa pieza entera, asi que la
    # fulfillment order de la parte de despiece tiene que terminar en el mismo almacen
    # que resuelva el tag final del pedido (el mismo `new_tags` ya calculado arriba, que
    # ya contempla el caso de split entre varios almacenes propios via la regla
    # TERRASA/MDP de mas arriba).
    #   - Si el pedido TODAVIA esta esperando (`waiting` no vacio -> tiene RESERVA/SIN
    #     STOCK) el despiece NO se mueve: recien cuando se sepa de donde sale la pieza
    #     entera tiene sentido decidir de donde sale el despiece (confirmado con
    #     Northdeco el 04/09 — "SI" al supuesto de dejarlo quieto hasta que se resuelva).
    #   - Si el pedido resuelve en un tag de almacen concreto (D10/TERRASA/MDP/AP,
    #     incluido el caso de split entre almacenes propios que colapsa a TERRASA/MDP
    #     via la misma regla de siempre — confirmado con Northdeco el 04/09, "SI" a que
    #     el despiece siga el tag final del pedido) se mueve ahi cualquier fulfillment
    #     order de despiece que no coincida ya, salvo que ese fo_id ya se este moviendo/
    #     partiendo por las lineas normales (`fo_destinos`, evita un move duplicado o
    #     contradictorio sobre el mismo fulfillment order).
    despiece_entries = r.get("despiece_lines", [])
    if despiece_entries and not waiting:
        final_wh_tags = new_tags & ALL_WH_TAGS
        if len(final_wh_tags) == 1:
            target_loc = REVERSE_WAREHOUSE_TAG[next(iter(final_wh_tags))]
            despiece_fo_seen = set()
            for dl in despiece_entries:
                dfo_id = dl["fo_id"]
                if dfo_id in fo_destinos or dfo_id in despiece_fo_seen:
                    continue
                despiece_fo_seen.add(dfo_id)
                if dl["fo_location"] != target_loc:
                    gd["moves"] += 1
                    gd["despiece_moves"] += 1
                    changed = True
                    ops_moves.append({"order": order["name"], "fo_id": dfo_id,
                                       "from": dl["fo_location"], "to": target_loc})

    if changed:
        gd["orders_changed"] += 1

print("=== resumen de diffs por grupo ===")
tot_orders_changed = 0
tot_despiece_moves = 0
for g in sorted(group_diff_counts.keys()):
    v = group_diff_counts[g]
    print(g, v)
    tot_orders_changed += v["orders_changed"]
    tot_despiece_moves += v.get("despiece_moves", 0)

print("TOTAL pedidos con algun cambio pendiente:", tot_orders_changed)
print("Total moves simples necesarios:", len(ops_moves), f"(de los cuales {tot_despiece_moves} son moves de despiece siguiendo al producto normal)")
print("Total pedidos que requieren split:", len(splits_needed))
print("Total operaciones tagsAdd:", len(ops_tagsAdd))
print("Total operaciones tagsRemove:", len(ops_tagsRemove))
print("ENVIO PARCIAL: altas", sum(PARTIAL_TAG in t["tags"] for t in ops_tagsAdd),
      "/ bajas", sum(PARTIAL_TAG in t["tags"] for t in ops_tagsRemove))
_solo_parcial = sum(1 for t in ops_tagsAdd + ops_tagsRemove if t["tags"] == [PARTIAL_TAG])
print("  operaciones de tags que son SOLO ENVIO PARCIAL:", _solo_parcial)

with open("diff.pkl", "wb") as f:
    pickle.dump({"moves": ops_moves, "tagsAdd": ops_tagsAdd, "tagsRemove": ops_tagsRemove, "splits": splits_needed}, f)
