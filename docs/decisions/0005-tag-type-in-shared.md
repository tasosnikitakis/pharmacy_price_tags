# 0005 `TagType` and `Palette` live in `shared/`

**Decision.** The renderer signature `render(label: LabelData, tag_type: TagType)` and
`TagDriver.push_image(..., tag_type: TagType, ...)` use the same `shared.tag_types.TagType`.

**Why.** Both sides need it, and the gateway must not import from `core/` (section 2:
the two processes only share the HTTP contract).
