"""保留公开JATS/HTML的章节、单元格、公式和引用，不把版面扁平化冒充完整。"""
def academic_blocks(soup, body, *, is_xml=False):
    blocks, section = [], ''
    roots = [body]
    if is_xml:
        article = soup.find('article')
        back = article.find('back', recursive=False) if article else None
        if back:
            roots.append(back)
    containers = {'p','li','table','disp-formula','inline-formula','caption','ref'}
    selectors = 'h1,h2,h3,h4,h5,h6,title,p,li,table,disp-formula,caption,ref'
    for root in roots:
        for node in root.select(selectors):
            if any(parent.name in containers for parent in node.parents if parent is not root):
                continue
            kind = 'paragraph'
            if node.name == 'table':
                rows = [[cell.get_text(' ', strip=True).replace('|','\\|') for cell in row.find_all(['td','th'], recursive=False)]
                        for row in node.find_all('tr')]
                value = '\n'.join('| ' + ' | '.join(row) + ' |' for row in rows if row)
                kind = 'table'
            elif node.name == 'disp-formula':
                tex = node.find(['tex-math','annotation'])
                value = tex.get_text(' ', strip=True) if tex else str(node.find('math') or node)
                kind = 'formula'
            else:
                value = node.get_text(' ', strip=True)
                if node.name == 'ref':
                    kind = 'reference'
                elif node.name == 'caption':
                    kind = 'caption'
                elif node.name in {'title','h1','h2','h3','h4','h5','h6'}:
                    section, kind = value, 'heading'
            if value:
                blocks.append({'text':value, 'section':section, 'kind':kind})
    return blocks
