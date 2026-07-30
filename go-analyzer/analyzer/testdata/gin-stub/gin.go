package gin

type H map[string]any
type HandlerFunc func(*Context)
type Context struct{}
type RouterGroup struct{}
type Engine struct{ RouterGroup }

func Default() *Engine { return &Engine{} }
func New() *Engine     { return &Engine{} }

func (*RouterGroup) GET(string, ...HandlerFunc)    {}
func (*RouterGroup) POST(string, ...HandlerFunc)   {}
func (*RouterGroup) PUT(string, ...HandlerFunc)    {}
func (*RouterGroup) DELETE(string, ...HandlerFunc) {}
func (*RouterGroup) Group(string) *RouterGroup     { return &RouterGroup{} }
func (*RouterGroup) Use(...HandlerFunc)            {}

func (*Context) Param(string) string      { return "" }
func (*Context) Query(string) string      { return "" }
func (*Context) ShouldBindJSON(any) error { return nil }
func (*Context) JSON(int, any)            {}
