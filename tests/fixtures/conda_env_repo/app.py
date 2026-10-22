import openai


def summarize(text):
    response = openai.Completion.create(model="gpt-3.5-turbo-instruct", prompt=text)
    return response["choices"][0]["text"]
