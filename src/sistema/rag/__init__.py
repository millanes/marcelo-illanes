"""RAG de manuales: los documentos de la planta (manuales de la bomba y del filtro, buenas prácticas de la
operadora, contrato e historial de órdenes) indexados en Postgres con pgvector, para buscar con cita.

    from sistema.rag import EmbebedorLocal, Manuales
    manuales = Manuales(planta.descargar, cfg.database_url, EmbebedorLocal(cfg.rag_modelo, cfg.rag_cache, 1))
    manuales.arrancar(parar)          # baja los documentos e indexa en un hilo, solo si cambiaron
    manuales.buscar("la bomba suena a piedras y la succión está baja", k=5)
    # [{"cita": "TX350-D §8.9", "documento": "TX350-D", "titulo": "TX-T07 Cavitación ...", "texto": ...,
    #   "vigente": True, "puntaje": 0.93, "fabricante": "Trimax Industrial S.A.", "equipos": [...], ...}]

Lo que resuelve (ver corpus.py y busqueda.py):
- la Rev. C del manual de la bomba está reemplazada: no sale, o sale abajo de la D con solo_vigentes=False;
- las secciones de otros modelos (TX-250, TX-500, FC-24, FC-96), de otras plantas (PIA-1, PIA-2), de opcionales
  y de instalación quedan marcadas (aplica, motivo) y bajan, salvo que la consulta las nombre;
- los códigos de falla del fabricante (TX-T07, FC-T02) se traducen a los de la planta (F01 a F10) con la tabla
  del MBP-4 §6, al indexar y en la consulta;
- los nombres de campo de los equipos ("la dos", "B3", "el primario") y la lista de precios que corresponde al
  año del trabajo.

Los límites de alarma y el stock NO salen de acá: los límites son los de GET /api/planta y el stock el del
almacén. Lo que diga un manual sobre eso es contexto, con su cita, no el dato.
"""

from .busqueda import Ajustes, Buscador, Embebedor, Lexico, VectoresMemoria, tokens
from .corpus import PLANTA, Corpus, Falla, Fragmento, preparar
from .embeddings import MODELO, EmbebedorLocal
from .postgres import BasePgvector, Manuales, VectoresPostgres, leer_archivos

__all__ = ["Ajustes", "BasePgvector", "Buscador", "Corpus", "EmbebedorLocal", "Embebedor", "Falla", "Fragmento",
           "Lexico", "MODELO", "Manuales", "PLANTA", "VectoresMemoria", "VectoresPostgres", "leer_archivos",
           "preparar", "tokens"]
