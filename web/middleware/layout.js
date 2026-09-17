// Every res.render goes through views/layout.ejs unless the call passes
// `layout: false`. Views describe themselves by mutating the per-request
// `page` object (it is shared by reference with the layout):
//
//   <% page.title = 'People'; page.nav = 'people'; page.css.push('/css/people.css') %>
//   <% page.js.push('/js/grid.js') %>
//   <% page.wide = true %>      full-width main (grids); default is a reading column
//
// Scripts are emitted as <script src defer> at the end of <body>.

function layoutMiddleware(req, res, next) {
  res.locals.page = { title: null, nav: null, css: [], js: [], wide: false, bodyClass: '' };
  res.locals.currentUrl = req.originalUrl;
  const render = res.render.bind(res);
  res.render = function renderWithLayout(view, options, cb) {
    if (typeof options === 'function') { cb = options; options = {}; }
    const opts = options || {};
    if (opts.layout === false) return render(view, opts, cb);
    render(view, opts, (err, html) => {
      if (err) return cb ? cb(err) : req.next(err);
      render('layout', { ...opts, body: html }, cb);
    });
  };
  next();
}

module.exports = { layoutMiddleware };
