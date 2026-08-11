"""Academic metadata providers for KnowledgeRadar."""

from .models import AcademicSearchRequest, AcademicSearchResponse, AcademicWork
from .ar5iv import Ar5ivProvider
from .arxiv import ArxivProvider
from .baidu_scholar import BaiduScholarProvider
from .calis_thesis import CalisThesisProvider
from .chinaxiv import ChinaXivProvider
from .citation_import import CitationImportProvider
from .coaj import CoajProvider
from .core import CoreProvider
from .crossref import CrossrefProvider
from .gooa import GoOaProvider
from .europepmc import EuropePmcProvider
from .hanspub import HansPubProvider
from .hkjo import HkjoProvider
from .ivy_publisher import IvyPublisherProvider
from .nssd import NssdProvider
from .nssd_cn import NssdCnProvider
from .nstrs import NstrsProvider
from .ntur import NturProvider
from .oajrc import OajrcProvider
from .oalib import OalibProvider
from .openalex import OpenAlexProvider
from .paper_edu import PaperEduProvider
from .paperscope import PaperScopeProvider
from .pubscholar import PubScholarProvider
from .sciopen import SciOpenProvider
from .sciengine import SciEngineProvider
from .semanticscholar import SemanticScholarProvider
from .serpapi_scholar import SerpApiScholarProvider
from .socolar import SocolarProvider
from .toaj import ToajProvider
from .ucdrs import UcdrsProvider
from .unpaywall import UnpaywallProvider
from .vip_oa import VipOpenAccessProvider

__all__ = [
    "AcademicSearchRequest",
    "AcademicSearchResponse",
    "AcademicWork",
    "Ar5ivProvider",
    "ArxivProvider",
    "BaiduScholarProvider",
    "CalisThesisProvider",
    "ChinaXivProvider",
    "CitationImportProvider",
    "CoajProvider",
    "CoreProvider",
    "CrossrefProvider",
    "EuropePmcProvider",
    "GoOaProvider",
    "HansPubProvider",
    "HkjoProvider",
    "IvyPublisherProvider",
    "NssdProvider",
    "NssdCnProvider",
    "NstrsProvider",
    "NturProvider",
    "OajrcProvider",
    "OalibProvider",
    "OpenAlexProvider",
    "PaperEduProvider",
    "PaperScopeProvider",
    "PubScholarProvider",
    "SciEngineProvider",
    "SciOpenProvider",
    "SemanticScholarProvider",
    "SerpApiScholarProvider",
    "SocolarProvider",
    "ToajProvider",
    "UcdrsProvider",
    "UnpaywallProvider",
    "VipOpenAccessProvider",
]
