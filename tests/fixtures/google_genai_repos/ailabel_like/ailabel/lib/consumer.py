from ailabel.lib.llms import get_gemini


def label(text):
    model = get_gemini()
    return model.generate_content(f"label: {text}").text
