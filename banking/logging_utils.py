def mask_identifier(value, *, prefix=8, suffix=4):
    if not value:
        return ''
    text = str(value)
    if len(text) <= prefix + suffix:
        return text
    return f'{text[:prefix]}...{text[-suffix:]}'
