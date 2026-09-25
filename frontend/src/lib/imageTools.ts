/**
 * Image tool names (PRP-0187, UDR-0169 D6).
 *
 * The tools are named category + verb. The legacy names are accepted ONLY when
 * READING stored history (rendering a result, labelling an old indicator): a stored
 * conversation keeps the name it was written with. Nothing sends a legacy name.
 */
export const IMAGE_GENERATE_TOOL = 'image_generate'
export const IMAGE_EDIT_TOOL = 'image_edit'

/** Every tool name whose result may carry generated images (current + legacy). */
export const IMAGE_RESULT_TOOLS: ReadonlySet<string> = new Set([
  IMAGE_GENERATE_TOOL,
  IMAGE_EDIT_TOOL,
  'generate_image',
  'edit_image',
])
