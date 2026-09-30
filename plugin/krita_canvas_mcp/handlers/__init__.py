# 方法注册表：HTTP RPC method → handler 函数（主线程执行）。
from . import (brush, fx, image_ops, io, node, observe, paint, vector,
               view_state)

HANDLERS = {
    # 观测
    "get_document_info": observe.get_document_info,
    "get_canvas_snapshot": observe.get_canvas_snapshot,
    "list_documents": observe.list_documents,
    "get_node_tree": node.get_node_tree,
    "get_node_pixels": image_ops.get_node_pixels,
    "list_channels": image_ops.list_channels,
    "get_channel_pixels": image_ops.get_channel_pixels,
    "set_channel_pixels": image_ops.set_channel_pixels,
    # 绘画
    "paint_line": paint.paint_line,
    "paint_path": paint.paint_path,
    "paint_shape": paint.paint_shape,
    "write_pixels": paint.write_pixels,
    "check_paintability": paint.check_paintability,
    "wait_for_done": paint.wait_for_done,
    # 笔刷/颜色
    "set_colors": brush.set_colors,
    "set_brush_params": brush.set_brush_params,
    "set_brush_preset": brush.set_brush_preset,
    "list_resources": brush.list_resources,
    "set_blending_mode": brush.set_blending_mode,
    "set_brush_flags": brush.set_brush_flags,
    "sample_color": brush.sample_color,
    "undo": brush.undo,
    "redo": brush.redo,
    # 节点
    "create_node": node.create_node,
    "create_fill_layer": node.create_fill_layer,
    "set_node_props": node.set_node_props,
    "manage_node": node.manage_node,
    "transform_node": fx.transform_node,
    # 选区
    "selection_op": image_ops.selection_op,
    "get_selection_pixels": image_ops.get_selection_pixels,
    "set_selection_pixels": image_ops.set_selection_pixels,
    # 矢量
    "vector_add_svg": vector.vector_add_svg,
    "vector_get_shapes": vector.vector_get_shapes,
    "vector_shape_op": vector.vector_shape_op,
    "vector_export_svg": vector.vector_export_svg,
    # 滤镜/文档变换
    "apply_filter": fx.apply_filter,
    "get_filter_config": fx.get_filter_config,
    "transform_document": fx.transform_document,
    # IO/应用
    "create_document": io.create_document,
    "open_document": io.open_document,
    "close_document": io.close_document,
    "save_document": io.save_document,
    "get_krita_info": io.get_krita_info,
    "execute_action": io.execute_action,
    "get_setting": io.get_setting,
    "set_setting": io.set_setting,
    # 视图
    "get_view_state": view_state.get_view_state,
    "set_view_state": view_state.set_view_state,
}