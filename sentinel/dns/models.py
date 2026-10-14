from dataclasses import dataclass


@dataclass
class BlocklistSource:
    name: str
    url: str
    category: str
