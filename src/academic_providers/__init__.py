"""学术注册入口；仅在确实使用某个来源时导入其实现。"""
from importlib import import_module
from .models import AcademicSearchRequest, AcademicSearchResponse, AcademicWork

_PROVIDERS = {'Ar5ivProvider': 'ar5iv', 'ArxivProvider': 'arxiv', 'CalisThesisProvider': 'calis_thesis', 'ChinaXivProvider': 'chinaxiv', 'CitationImportProvider': 'citation_import', 'CoreProvider': 'core', 'CrossrefProvider': 'crossref', 'EuropePmcProvider': 'europepmc', 'HkjoProvider': 'hkjo', 'NssdProvider': 'nssd', 'NapsticOaProvider': 'napstic_oa', 'NstrsProvider': 'nstrs', 'NturProvider': 'ntur', 'OpenAlexProvider': 'openalex', 'PubScholarProvider': 'pubscholar', 'SciEngineProvider': 'sciengine', 'SemanticScholarProvider': 'semanticscholar', 'SerpApiScholarProvider': 'serpapi_scholar', 'ToajProvider': 'toaj', 'UnpaywallProvider': 'unpaywall', 'VipOpenAccessProvider': 'vip_oa', 'WanfangProvider': 'wanfang'}
__all__ = ['AcademicSearchRequest', 'AcademicSearchResponse', 'AcademicWork', *_PROVIDERS]


def __getattr__(name):
    if name not in _PROVIDERS:
        raise AttributeError(name)
    value = getattr(import_module('.' + _PROVIDERS[name], __name__), name)
    globals()[name] = value
    return value
