from __future__ import annotations

from typing import Any, Type


def model_dump(instance: Any) -> dict[str, Any]:
    if hasattr(instance, "model_dump"):
        return instance.model_dump()
    return instance.dict()



def model_copy(instance: Any, **kwargs: Any) -> Any:
    if hasattr(instance, "model_copy"):
        return instance.model_copy(**kwargs)
    return instance.copy(**kwargs)



def model_validate(model_cls: Type[Any], data: Any) -> Any:
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(data)
    return model_cls.parse_obj(data)



def model_construct(model_cls: Type[Any], **kwargs: Any) -> Any:
    if hasattr(model_cls, "model_construct"):
        return model_cls.model_construct(**kwargs)
    return model_cls.construct(**kwargs)
