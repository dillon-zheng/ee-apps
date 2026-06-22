package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"goa.design/clue/health"
	goahttp "goa.design/goa/v3/http"
	httpmdlwr "goa.design/goa/v3/http/middleware"
	"goa.design/goa/v3/middleware"
	goa "goa.design/goa/v3/pkg"

	ks3svr "github.com/PingCAP-QE/ee-apps/dl/gen/http/ks3/server"
	ocisvr "github.com/PingCAP-QE/ee-apps/dl/gen/http/oci/server"
	ks3 "github.com/PingCAP-QE/ee-apps/dl/gen/ks3"
	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
)

// handleHTTPServer starts configures and starts a HTTP server on the given
// URL. It shuts down the server if any error is received in the error channel.
func handleHTTPServer(ctx context.Context, u *url.URL, ociEndpoints *oci.Endpoints, ks3Endpoints *ks3.Endpoints, wg *sync.WaitGroup, errc chan error, logger *log.Logger, debug bool, ociSvc oci.Service) {

	// Setup goa log adapter.
	var (
		adapter middleware.Logger
	)
	{
		adapter = middleware.NewLogger(logger)
	}

	// Provide the transport specific request decoder and response encoder.
	// The goa http package has built-in support for JSON, XML and gob.
	// Other encodings can be used by providing the corresponding functions,
	// see goa.design/implement/encoding.
	var (
		dec = goahttp.RequestDecoder
		enc = goahttp.ResponseEncoder
	)

	// Build the service HTTP request multiplexer and configure it to serve
	// HTTP requests to the service endpoints.
	var mux goahttp.Muxer
	{
		mux = goahttp.NewMuxer()
	}

	// Wrap the endpoints with the transport specific layers. The generated
	// server packages contains code generated from the design which maps
	// the service input and output data structures to HTTP requests and
	// responses.
	var (
		ociServer *ocisvr.Server
		ks3Server *ks3svr.Server
	)
	{
		eh := errorHandler(logger)
		ociServer = ocisvr.New(ociEndpoints, mux, dec, enc, eh, nil)
		ks3Server = ks3svr.New(ks3Endpoints, mux, dec, enc, eh, nil)
		if debug {
			servers := goahttp.Servers{
				ociServer,
				ks3Server,
			}
			servers.Use(httpmdlwr.Debug(mux, os.Stdout))
		}
	}
	// Configure the mux.
	ocisvr.Mount(mux, ociServer)
	ks3svr.Mount(mux, ks3Server)

	// ** Mount health check handler **
	check := health.Handler(health.NewChecker())
	mux.Handle("GET", "/healthz", check)
	mux.Handle("GET", "/livez", check)

	// Wrap the multiplexer with additional middlewares. Middlewares mounted
	// here apply to all the service endpoints.
	var handler http.Handler = mux
	{
		handler = headOCIMiddleware(ociSvc, logger)(handler)
		handler = httpmdlwr.Log(adapter)(handler)
		handler = httpmdlwr.RequestID()(handler)
	}

	// Start HTTP server using default configuration, change the code to
	// configure the server as required by your service.
	srv := &http.Server{Addr: u.Host, Handler: handler, ReadHeaderTimeout: time.Second * 60}
	for _, m := range ociServer.Mounts {
		logger.Printf("HTTP %q mounted on %s %s", m.Method, m.Verb, m.Pattern)
	}
	for _, m := range ks3Server.Mounts {
		logger.Printf("HTTP %q mounted on %s %s", m.Method, m.Verb, m.Pattern)
	}

	(*wg).Add(1)
	go func() {
		defer (*wg).Done()

		// Start HTTP server in a separate goroutine.
		go func() {
			logger.Printf("HTTP server listening on %q", u.Host)
			errc <- srv.ListenAndServe()
		}()

		<-ctx.Done()
		logger.Printf("shutting down HTTP server at %q", u.Host)

		// Shutdown gracefully with a 30s timeout.
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		err := srv.Shutdown(ctx)
		if err != nil {
			logger.Printf("failed to shutdown: %v", err)
		}
	}()
}

// errorHandler returns a function that writes and logs the given error.
// The function also writes and logs the error unique ID so that it's possible
// to correlate.
func errorHandler(logger *log.Logger) func(context.Context, http.ResponseWriter, error) {
	return func(ctx context.Context, w http.ResponseWriter, err error) {
		id := ctx.Value(middleware.RequestIDKey).(string)
		_, _ = w.Write([]byte("[" + id + "] encoding: " + err.Error()))
		logger.Printf("[%s] ERROR: %s", id, err.Error())
	}
}

// headOCIMiddleware intercepts HEAD requests to /oci-file/{*repository} and
// checks file existence without downloading the blob, enabling wget --spider.
func headOCIMiddleware(ociSvc oci.Service, logger *log.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodHead {
				next.ServeHTTP(w, r)
				return
			}

			if !strings.HasPrefix(r.URL.Path, "/oci-file/") {
				next.ServeHTTP(w, r)
				return
			}

			repository := strings.TrimPrefix(r.URL.Path, "/oci-file/")
			if repository == "" {
				http.Error(w, "missing repository", http.StatusBadRequest)
				return
			}

			qp := r.URL.Query()
			tag := qp.Get("tag")
			if tag == "" {
				http.Error(w, "missing tag query parameter", http.StatusBadRequest)
				return
			}

			payload := &oci.HeadFilePayload{
				Repository: repository,
				Tag:        tag,
			}
			if file := qp.Get("file"); file != "" {
				payload.File = &file
			}
			if fileRegex := qp.Get("file_regex"); fileRegex != "" {
				payload.FileRegex = &fileRegex
			}
			if payload.File == nil && payload.FileRegex == nil {
				http.Error(w, "missing file or file_regex parameter", http.StatusBadRequest)
				return
			}

			result, err := ociSvc.HeadFile(r.Context(), payload)
			if err != nil {
				status, message := headOCIErrorResponse(err)
				logger.Printf("HEAD oci-file: HeadFile: %v", err)
				http.Error(w, message, status)
				return
			}

			w.Header().Set("Content-Disposition", result.ContentDisposition)
			w.Header().Set("Content-Length", fmt.Sprintf("%d", result.Length))
			w.Header().Set("Content-Type", "application/octet-stream")
			w.WriteHeader(http.StatusOK)
		})
	}
}

func headOCIErrorResponse(err error) (int, string) {
	var serviceErr *goa.ServiceError
	if errors.As(err, &serviceErr) {
		if serviceErr.Name == "invalid_file_path" {
			message := serviceErr.ErrorName()
			switch {
			case strings.Contains(message, "none `file` or `file_regex` param given"):
				return http.StatusBadRequest, "missing file or file_regex parameter"
			case strings.Contains(message, "error parsing regexp"):
				return http.StatusBadRequest, "invalid file_regex"
			default:
				return http.StatusNotFound, "file not found"
			}
		}
	}
	return http.StatusInternalServerError, "failed to resolve repository"
}
