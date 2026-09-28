"""Graph package entry point (lazy to avoid Cat2 subgraph import cycles)."""

__all__ = ["SupplyDataQualityPrecedentGraph"]


def __getattr__(name: str) -> object:
    if name == "SupplyDataQualityPrecedentGraph":
        from src.graph.graph import SupplyDataQualityPrecedentGraph

        return SupplyDataQualityPrecedentGraph
    raise AttributeError(name)
