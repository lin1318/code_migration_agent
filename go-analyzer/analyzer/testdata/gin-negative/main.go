package negative

type context struct{}

func (context) JSON(int, any) {}
func (context) Param(string)  {}
func (context) Query(string)  {}

type engine struct{}

func (engine) Default() engine              { return engine{} }
func (engine) GET(string, ...func(context)) {}
func (engine) Use(...func(context))         {}

func unrelated() {
	gin := engine{}
	router := gin.Default()
	router.GET("/", func(c context) {
		c.JSON(200, map[string]any{"not": "gin"})
		c.Param("id")
		c.Query("q")
	})
	router.Use(func(context) {})
}
