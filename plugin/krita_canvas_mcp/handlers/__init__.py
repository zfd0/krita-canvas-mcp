from . import observe

HANDLERS = {
    "get_document_info": observe.get_document_info,
    "get_canvas_snapshot": observe.get_canvas_snapshot,
}