from docingest.domain.models import SourceMetadata


def test_citation_formats_arxiv_metadata():
    m = SourceMetadata(
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
        arxiv_id="1706.03762",
        version="v7",
    )
    assert m.citation("x") == "Vaswani et al. (2017). Attention Is All You Need. arXiv:1706.03762v7"


def test_citation_falls_back_gracefully():
    assert SourceMetadata().citation("paper.pdf") == "paper.pdf"
