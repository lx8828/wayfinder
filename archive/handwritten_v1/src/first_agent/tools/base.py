tools = []

import re
import inspect


def _parse_docstring_args(docstring):
    """从 Google 风格 docstring 提取参数描述，返回 {参数名: 描述}"""

    if not docstring:
        return {}

    match = re.search(r'Args:\s*\n(.*?)(?:\n\n|\Z)', docstring, re.DOTALL)

    if not match:
        return {}

    params = {}

    for line in match.group(1).strip().split('\n'):

        line = line.strip()

        if ':' in line:

            name, desc = line.split(':', 1)

            params[name.strip()] = desc.strip()

    return params


def tool(description=""):

    def decorator(func):

        func.tool_description = description

        tools.append(func)

        return func

    return decorator


def get_tool_schemas():

    from first_agent.tools import weather, time

    schemas=[]


    for func in tools:


        signature = inspect.signature(func)

        param_descs = _parse_docstring_args(func.__doc__)


        properties={}

        required=[]


        for name,param in signature.parameters.items():

            param_type="string"


            if param.annotation == int:
                param_type="integer"

            elif param.annotation == float:
                param_type="number"

            elif param.annotation == bool:
                param_type="boolean"


            properties[name]={
                "type":param_type,
                "description":param_descs.get(name, "")
            }


            required.append(name)


        schemas.append(

            {
                "type":"function",

                "function":{

                    "name":func.__name__,

                    "description":
                    func.tool_description,


                    "parameters":{

                        "type":"object",

                        "properties":properties,

                        "required":required

                    }

                }

            }

        )


    return schemas