from langchain_anthropic import ChatAnthropic
from dotenv import load_dotenv

load_dotenv()

def pick_llm(level:str):
    """
    Picks the appropriate LLM based on the level of the question

    args: 
        level(str): the level of question, can be easy, medium or hard
    
    returns:
        str: The name of the LLM to be used 
    """

    if level.lower() == "low":
        llm = ChatAnthropic(model_name="claude-haiku-4-5", temperature=0)
    elif level.lower() == "medium":
        llm = ChatAnthropic(model_name="claude-sonnet-5", temperature=0)
    elif level.lower() == "high":
        llm = ChatAnthropic(model_name="claude-opus-5", temperature=0)
    else:
        raise ValueError(f"Unsupported level: {level}")

    return llm
