import bpy
import bmesh
import math
from bpy_extras import view3d_utils
from collections import deque
from mathutils import Vector, Matrix
from mathutils.geometry import intersect_line_plane, distance_point_to_plane
from mathutils.bvhtree import BVHTree
from . import sprytile_utils


def get_grid_pos(position, grid_center, right_vector, up_vector, world_pixels, grid_x, grid_y):
    position_vector = position - grid_center
    pos_vector_normalized = position.normalized()

    if right_vector.dot(pos_vector_normalized) < 0:
        right_vector *= -1
    if up_vector.dot(pos_vector_normalized) < 0:
        up_vector *= -1

    x_magnitude = position_vector.dot(right_vector)
    y_magnitude = position_vector.dot(up_vector)

    x_unit = grid_x / world_pixels
    y_unit = grid_y / world_pixels

    x_snap = math.floor(x_magnitude / x_unit)
    y_snap = math.floor(y_magnitude / y_unit)

    right_vector *= x_unit
    up_vector *= y_unit

    grid_pos = grid_center + (right_vector * x_snap) + (up_vector * y_snap)

    return grid_pos, right_vector, up_vector


def snap_vector_to_axis(vector, mirrored=False):
    """Snaps a vector to the closest world axis"""
    norm_vector = vector.normalized()

    x = Vector((1.0, 0.0, 0.0))
    y = Vector((0.0, 1.0, 0.0))
    z = Vector((0.0, 0.0, 1.0))

    x_dot = 1 - abs(norm_vector.dot(x))
    y_dot = 1 - abs(norm_vector.dot(y))
    z_dot = 1 - abs(norm_vector.dot(z))
    dot_array = [x_dot, y_dot, z_dot]
    closest = min(dot_array)

    if closest is dot_array[0]:
        snapped_vector = x
    elif closest is dot_array[1]:
        snapped_vector = y
    else:
        snapped_vector = z

    vector_dot = norm_vector.dot(snapped_vector)
    if mirrored is False and vector_dot < 0:
        snapped_vector *= -1
    elif mirrored is True and vector_dot > 0:
        snapped_vector *= -1

    return snapped_vector


def raycast_grid(scene, grid_id, up_vector, right_vector, plane_normal, ray_origin, ray_vector):
    """Finds the grid position"""

    plane_pos = intersect_line_plane(ray_origin, ray_origin + ray_vector, scene.cursor_location, plane_normal)
    # Didn't hit the plane exit
    if plane_pos is None:
        return None, None, None, None

    world_pixels = scene.sprytile_data.world_pixels
    target_grid = scene.sprytile_grids[grid_id]
    grid_x = target_grid.grid[0]
    grid_y = target_grid.grid[1]

    grid_position, x_vector, y_vector = get_grid_pos(plane_pos, scene.cursor_location,
                                                     right_vector.copy(), up_vector.copy(),
                                                     world_pixels, grid_x, grid_y)
    return grid_position, x_vector, y_vector, plane_pos


def get_current_grid_vectors(scene):
    """Returns the current grid X/Y/Z vectors from scene data"""
    data_normal = scene.sprytile_data.paint_normal_vector
    data_up_vector = scene.sprytile_data.paint_up_vector

    normal_vector = Vector((data_normal[0], data_normal[1], data_normal[2]))
    up_vector = Vector((data_up_vector[0], data_up_vector[1], data_up_vector[2]))

    normal_vector.normalize()
    up_vector.normalize()
    right_vector = up_vector.cross(normal_vector)

    return up_vector, right_vector, normal_vector


def uv_map_face(context, up_vector, right_vector, tile_xy, face_index, mesh=None):
    """UV map the given face"""
    scene = context.scene
    obj = context.object
    data = scene.sprytile_data

    grid_id = obj.sprytile_gridid
    target_grid = scene.sprytile_grids[grid_id]

    # Generate a transform matrix from the grid settings
    if mesh is None:
        mesh = bmesh.from_edit_mesh(obj.data)

    world_units = data.world_pixels
    world_convert = Vector((target_grid.grid[0] / world_units,
                            target_grid.grid[1] / world_units))
    uv_layer = mesh.loops.layers.uv.verify()
    mesh.faces.layers.tex.verify()

    if face_index >= len(mesh.faces):
        return

    target_img = sprytile_utils.get_grid_texture(target_grid)
    if target_img is None:
        return

    pixel_uv_x = 1.0 / target_img.size[0]
    pixel_uv_y = 1.0 / target_img.size[1]
    uv_unit_x = pixel_uv_x * target_grid.grid[0]
    uv_unit_y = pixel_uv_y * target_grid.grid[1]

    # Build the translation matrix
    uv_matrix = Matrix.Translation((uv_unit_x * tile_xy[0], uv_unit_y * tile_xy[1], 0))

    face = mesh.faces[face_index]
    vert_origin = face.calc_center_median()
    for loop in face.loops:
        # Project the vert position onto UV space
        # using up and right vectors
        vert = loop.vert
        vert_pos = vert.co - vert_origin
        vert_xy = (right_vector.dot(vert_pos), up_vector.dot(vert_pos), 0)
        vert_xy = Vector(vert_xy)
        vert_xy.x /= world_convert.x
        vert_xy.y /= world_convert.y
        vert_xy += Vector((0.5, 0.5, 0))
        # and then apply the grid transform matrix
        vert_xy.x *= uv_unit_x
        vert_xy.y *= uv_unit_y
        vert_xy = uv_matrix * vert_xy
        loop[uv_layer].uv = vert_xy.xy
    bmesh.update_edit_mesh(obj.data)
    mesh.faces.index_update()
    return face.index, target_grid


class SprytileModalTool(bpy.types.Operator):
    """Tile based mesh creation/UV layout tool"""
    bl_idname = "sprytile.modal_tool"
    bl_label = "Sprytile Paint"

    def find_view_axis(self, context):
        scene = context.scene
        if scene.sprytile_data.lock_normal is True:
            return

        region = context.region
        rv3d = context.region_data

        # Get the view ray from center of screen
        coord = Vector((int(region.width / 2), int(region.height / 2)))
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

        # Get the up vector. The default scene view camera is pointed
        # downward, with up on Y axis. Apply view rotation to get current up
        view_up_vector = rv3d.view_rotation * Vector((0.0, 1.0, 0.0))
        # print("view up", view_up_vector)
        # print("Original forward", rv3d.view_rotation.inverted() * view_vector)

        plane_normal = snap_vector_to_axis(view_vector, mirrored=True)
        up_vector = snap_vector_to_axis(view_up_vector)

        # calculated vectors are not perpendicular, don't set data
        if plane_normal.dot(up_vector) != 0.0:
            return

        scene.sprytile_data.paint_normal_vector = plane_normal
        scene.sprytile_data.paint_up_vector = up_vector

        if abs(plane_normal.x) > 0:
            new_mode = 'X'
        elif abs(plane_normal.y) > 0:
            new_mode = 'Y'
        else:
            new_mode = 'Z'

        if new_mode != scene.sprytile_data.normal_mode:
            self.virtual_cursor.clear()
        scene.sprytile_data.normal_mode = new_mode

    def add_virtual_cursor(self, cursor_pos):
        cursor_len = len(self.virtual_cursor)
        if cursor_len == 0:
            self.virtual_cursor.append(cursor_pos)
            return

        last_pos = self.virtual_cursor[cursor_len-1]
        last_vector = cursor_pos - last_pos
        if last_vector.magnitude < 0.1:
            return

        # If last vector goes in different direction
        # from current virtual cursor vector, reset cursor
        cursor_vector = self.get_virtual_cursor_vector()
        if cursor_vector.dot(last_vector) < 0:
            self.virtual_cursor.clear()

        self.virtual_cursor.append(cursor_pos)

    def get_virtual_cursor_vector(self):
        cursor_direction = Vector((0.0, 0.0, 0.0))
        cursor_len = len(self.virtual_cursor)
        if cursor_len <= 1:
            return cursor_direction
        for idx in range(cursor_len - 1):
            segment = self.virtual_cursor[idx + 1] - self.virtual_cursor[idx]
            cursor_direction += segment
        cursor_direction /= cursor_len
        return cursor_direction

    def flow_cursor(self, context, face_index, virtual_cursor):
        """Move the cursor along the given face, using virtual_cursor direction"""
        cursor_len = len(self.virtual_cursor)
        if cursor_len <= 1:
            return

        cursor_direction = self.get_virtual_cursor_vector()
        cursor_direction.normalize()

        face = self.bmesh.faces[face_index]
        max_dot = 1.0
        closest_idx = -1
        closest_pos = Vector((0.0, 0.0, 0.0))
        for idx, vert in enumerate(face.verts):
            vert_world_pos = context.object.matrix_world * vert.co
            vert_vector = vert_world_pos - virtual_cursor
            vert_vector.normalize()
            vert_dot = abs(1.0 - vert_vector.dot(cursor_direction))
            if vert_dot < max_dot:
                closest_idx = idx
                closest_pos = vert_world_pos

        if closest_idx != -1:
            context.scene.cursor_location = closest_pos

    def raycast_object(self, obj, ray_origin, ray_direction):
        matrix = obj.matrix_world.copy()
        # get the ray relative to the object
        matrix_inv = matrix.inverted()
        ray_origin_obj = matrix_inv * ray_origin
        ray_target_obj = matrix_inv * (ray_origin + ray_direction)
        ray_direction_obj = ray_target_obj - ray_origin_obj

        location, normal, face_index, distance = self.tree.ray_cast(ray_origin_obj, ray_direction_obj)
        if face_index is None:
            return None, None, None, None
        # Translate location back to world space
        location = matrix * location
        return location, normal, face_index, distance

    def execute_tool(self, context, event):
        """Run the paint tool"""
        # Don't do anything if nothing to raycast on
        # or the GL GUI is using the mouse
        if self.tree is None or context.scene.sprytile_data.gui_use_mouse is True:
            return

        # print("Execute tool")
        # get the context arguments
        scene = context.scene
        region = context.region
        rv3d = context.region_data
        coord = event.mouse_region_x, event.mouse_region_y

        # get the ray from the viewport and mouse
        ray_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)

        # if paint mode, ray cast against object
        paint_mode = scene.sprytile_data.paint_mode
        if paint_mode == 'PAINT':
            self.execute_paint(context, ray_origin, ray_vector)
        # if build mode, ray cast on plane and build face
        elif paint_mode == 'MAKE_FACE':
            self.execute_build(context, scene, ray_origin, ray_vector)
            # set normal mode...

    def execute_paint(self, context, ray_origin, ray_vector):
        up_vector, right_vector, plane_normal = get_current_grid_vectors(context.scene)
        hit_loc, normal, face_index, distance = self.raycast_object(context.object, ray_origin, ray_vector)
        if face_index is not None:
            self.add_virtual_cursor(hit_loc)
            # Change the uv of the given face
            grid = context.scene.sprytile_grids[context.object.sprytile_gridid]
            tile_xy = (grid.tile_selection[0], grid.tile_selection[1])
            face_index, grid = uv_map_face(context, up_vector, right_vector, tile_xy, face_index, self.bmesh)
            mat_id = bpy.data.materials.find(grid.mat_id)
            if mat_id > -1:
                self.bmesh.faces[face_index].material_index = mat_id

    def execute_build(self, context, scene, ray_origin, ray_vector):
        grid = context.scene.sprytile_grids[context.object.sprytile_gridid]
        tile_xy = (grid.tile_selection[0], grid.tile_selection[1])

        up_vector, right_vector, plane_normal = get_current_grid_vectors(scene)
        hit_loc, hit_normal, face_index, hit_dist = self.raycast_object(context.object, ray_origin, ray_vector)

        # If raycast on the mesh, check that the hit face isn't facing
        # the same way as the plane_normal and not coplanar to target plane
        if face_index is not None:
            check_dot = plane_normal.dot(hit_normal)
            check_dot -= 1
            check_coplanar = distance_point_to_plane(hit_loc, scene.cursor_location, plane_normal)

            # print("Hit face")
            # print("Dot:", check_dot, " Coplanar", check_coplanar)

            check_coplanar = abs(check_coplanar) < 0.05
            check_dot = abs(check_dot) < 0.05
            if check_dot and check_coplanar:
                self.add_virtual_cursor(hit_loc)
                # Change UV of this face instead
                uv_map_face(context, up_vector, right_vector, tile_xy, face_index, self.bmesh)
                if scene.sprytile_data.cursor_flow:
                    self.flow_cursor(context, face_index, hit_loc)
                return

        face_position, x_vector, y_vector, plane_cursor = raycast_grid(
            scene, context.object.sprytile_gridid,
            up_vector, right_vector, plane_normal,
            ray_origin, ray_vector)
        if face_position is None:
            return

        # print("Execute build")
        # store plane_cursor, for deciding where to move actual cursor
        # if auto cursor mode is on
        self.add_virtual_cursor(plane_cursor)
        # Build face and UV map it
        face_index = self.build_face(context, face_position, x_vector, y_vector, up_vector, right_vector)
        uv_map_face(context, up_vector, right_vector, tile_xy, face_index, self.bmesh)
        if scene.sprytile_data.cursor_flow:
            self.flow_cursor(context, face_index, plane_cursor)
        # print("Build face")

    def build_face(self, context, position, x_vector, y_vector, up_vector, right_vector):
        """Build a face at the given position"""
        # Convert world space position to object space
        face_position = context.object.matrix_world.copy().inverted() * position

        x_dot = right_vector.dot(x_vector.normalized())
        y_dot = up_vector.dot(y_vector.normalized())
        x_positive = x_dot > 0
        y_positive = y_dot > 0

        vtx1 = self.bmesh.verts.new(face_position)
        vtx2 = self.bmesh.verts.new(face_position + y_vector)
        vtx3 = self.bmesh.verts.new(face_position + x_vector + y_vector)
        vtx4 = self.bmesh.verts.new(face_position + x_vector)

        # Quadrant II, IV
        face_order = (vtx1, vtx2, vtx3, vtx4)
        # Quadrant I, III
        if x_positive == y_positive:
            face_order = (vtx1, vtx4, vtx3, vtx2)

        face = self.bmesh.faces.new(face_order)
        face.normal_update()

        self.bmesh.faces.index_update()
        self.bmesh.faces.ensure_lookup_table()

        bmesh.update_edit_mesh(context.object.data, True, True)

        # Update the collision BVHTree with new data
        self.tree = BVHTree.FromBMesh(self.bmesh)
        return face.index

    def cursor_snap(self, context, event):
        if self.tree is None or context.scene.sprytile_data.gui_use_mouse is True:
            return

        # get the context arguments
        scene = context.scene
        region = context.region
        rv3d = context.region_data
        coord = event.mouse_region_x, event.mouse_region_y

        # get the ray from the viewport and mouse
        ray_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)

        up_vector, right_vector, plane_normal = get_current_grid_vectors(scene)

        # Snap cursor, depending on setting
        if scene.sprytile_data.cursor_snap == 'GRID':
            location = intersect_line_plane(ray_origin, ray_origin + ray_vector, scene.cursor_location, plane_normal)
            if location is None:
                return
            world_pixels = scene.sprytile_data.world_pixels
            target_grid = scene.sprytile_grids[context.object.sprytile_gridid]
            grid_x = target_grid.grid[0]
            grid_y = target_grid.grid[1]

            grid_position, x_vector, y_vector = get_grid_pos(location, scene.cursor_location,
                                                             right_vector.copy(), up_vector.copy(),
                                                             world_pixels, grid_x, grid_y)
            scene.cursor_location = grid_position

        elif scene.sprytile_data.cursor_snap == 'VERTEX':
            location, normal, face_index, distance = self.raycast_object(context.object, ray_origin, ray_vector)
            if location is None:
                return
            # Location in world space, convert to object space
            matrix = context.object.matrix_world.copy()
            matrix_inv = matrix.inverted()
            location, normal, face_index, dist = self.tree.find_nearest(matrix_inv * location)
            if location is None:
                return

            # Found the nearest face, go to BMesh to find the nearest vertex
            bm = bmesh.from_edit_mesh(context.object.data)
            face = bm.faces[face_index]
            closest_vtx = -1
            closest_dist = float('inf')
            # positions are in object space
            for vtx_idx, vertex in enumerate(face.verts):
                test_dist = (location - vertex.co).magnitude
                if test_dist < closest_dist:
                    closest_vtx = vtx_idx
                    closest_dist = test_dist
            # convert back to world space
            if closest_vtx != -1:
                scene.cursor_location = matrix * face.verts[closest_vtx].co

    def modal(self, context, event):
        context.area.tag_redraw()
        if event.type == 'TIMER':
            self.find_view_axis(context)
            return {'PASS_THROUGH'}

        if self.refresh_mesh:
            self.bmesh = bmesh.from_edit_mesh(context.object.data)
            self.tree = BVHTree.FromBMesh(self.bmesh)
            self.refresh_mesh = False

        region = context.region
        coord = Vector((event.mouse_region_x, event.mouse_region_y))
        # Pass through if outside the region
        if coord.x < 0 or coord.y < 0 or coord.x > region.width or coord.y > region.height:
            context.window.cursor_set('DEFAULT')
            return {'PASS_THROUGH'}

        context.window.cursor_set('PAINT_BRUSH')

        key_return = self.handle_keys(context, event)
        if key_return is not None:
            return key_return

        mouse_return = self.handle_mouse(context, event)
        if mouse_return is not None:
            return mouse_return

        return {'RUNNING_MODAL'}

    def handle_mouse(self, context, event):
        """"""
        if 'MOUSE' not in event.type:
            return None

        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            # allow navigation
            return {'PASS_THROUGH'}
        elif event.type == 'LEFTMOUSE':
            self.left_down = event.value == 'PRESS'
            if self.left_down:
                self.tree = BVHTree.FromBMesh(bmesh.from_edit_mesh(context.object.data))
                self.execute_tool(context, event)
            else:  # Mouse up, send undo
                bpy.ops.ed.undo_push()
            return {'RUNNING_MODAL'}
        elif event.type == 'MOUSEMOVE':
            # Update the event for the gui system
            if self.left_down:
                self.execute_tool(context, event)
                return {'RUNNING_MODAL'}
            if self.want_snap:
                self.cursor_snap(context, event)
        elif event.type in {'RIGHTMOUSE', 'ESC'} and context.scene.sprytile_data.gui_use_mouse is False:
            self.exit_modal(context)
            return {'CANCELLED'}

    def handle_keys(self, context, event):
        """Process keyboard presses"""

        def get_key(key_code):
            if key_code not in self.key_trap:
                self.key_trap[key_code] = False
            return self.key_trap[key_code]

        # Keys we're interested in
        key_dict = {
            'Z',
            'Y'
        }
        if event.type in key_dict:
            self.key_trap[event.type] = event.value == 'PRESS'

        # Check for undo commands, pass through the keystroke
        pass_undo_keys = get_key('Z') or get_key('Y')
        if event.ctrl and pass_undo_keys:
            self.refresh_mesh = True
            return {'PASS_THROUGH'}

        if event.type == 'S':
            self.want_snap = event.value == 'PRESS'
        elif event.type == 'C' and event.value == 'PRESS':
            bpy.ops.view3d.view_center_cursor('INVOKE_DEFAULT')

        return None

    def invoke(self, context, event):
        if context.space_data.type == 'VIEW_3D':
            obj = context.object
            if obj.hide or obj.type != 'MESH':
                self.report({'WARNING'}, "Active object must be a visible mesh")
                return {'CANCELLED'}

            self.virtual_cursor = deque([], 3)
            self.key_trap = {}
            # Set up for raycasting with a BVHTree
            self.left_down = False
            self.want_snap = False
            self.bmesh = bmesh.from_edit_mesh(context.object.data)
            self.tree = BVHTree.FromBMesh(self.bmesh)
            self.refresh_mesh = False

            # Set up timer callback
            self.view_axis_timer = context.window_manager.event_timer_add(0.1, context.window)

            context.window_manager.modal_handler_add(self)
            context.scene.sprytile_data.is_running = True
            bpy.ops.sprytile.gui_win('INVOKE_DEFAULT')
            return {'RUNNING_MODAL'}
        else:
            self.report({'WARNING'}, "Active space must be a View3d")
            return {'CANCELLED'}

    def exit_modal(self, context):
        context.scene.sprytile_data.is_running = False
        self.tree = None
        self.key_trap = {}
        context.window.cursor_set('DEFAULT')
        context.window_manager.event_timer_remove(self.view_axis_timer)
        bmesh.update_edit_mesh(context.object.data, True, True)


def register():
    bpy.utils.register_module(__name__)


def unregister():
    bpy.utils.unregister_module(__name__)


if __name__ == '__main__':
    register()
