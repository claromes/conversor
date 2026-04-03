# Conversor

.pdf to .epub for Xteink X4

## dev

- git clone
- create a virtual env
- `pip install pymupdf ebooklib Pillow`

## cmd

```python conversor.py path-to.pdf -t 'Title' -a 'Author name' -c path-to-cover.jpg```

### options

- "-o", "--output" (current dir is the default)
- "-c", "--cover" (.jpg or .png)
- "-t", "--title"
- "-a", "--author"

## todo

- digitized books as pdf files
